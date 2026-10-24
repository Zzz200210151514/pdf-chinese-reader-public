"""Compile reviewed V4 source maps to the existing in-place PDF manifest."""
from __future__ import annotations

from copy import deepcopy
import html
import re

from .classifier import classify, column_groups, intersects, union
from .redaction import split_masks
from .citations import (
    author_year_colours, canonical, caption_title_ink, dominant_link_blue,
    object_colours, restore_citations, usable_link_hex,
)
from .paragraphs import prepare_render_text


class LayoutError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("Layout needs review: "+"; ".join(f"{i['check']} ({i.get('id', i.get('page', 'map'))})" for i in issues))


def create_source_map(inspection):
    """Create a reviewable legacy map; review remains an explicit caller action."""
    return classify(inspection)


def _markup(text, known, colours=None, citation_tokens=(), author_years=None,
            link_blue=None, caption_key=None, caption_ink=None):
    colours = colours or {}
    author_years = author_years or {}
    link_blue = usable_link_hex(link_blue)
    caption_open = bool(caption_key and caption_ink)
    def anchor(key, label, colour=None):
        # Missing or non-blue source ink uses the paper's dominant link blue.
        # Pure #0000ff is never invented here.
        if colour is None:
            colour = usable_link_hex(colours.get(key)) or link_blue
        if colour:
            style = f'color:{colour};text-decoration:none'
        else:
            style = 'text-decoration:none'
        return f'<a href="https://pdf-reader.invalid/{key}" style="{style}">{label}</a>'
    def linked(key, label):
        nonlocal caption_open
        # The caption's own title keeps body ink. Later mentions are cross-refs.
        if caption_open and key == caption_key:
            caption_open = False
            return anchor(key, label, colour=caption_ink)
        return anchor(key, label)
    escaped = html.escape(text).replace("\n", "<br/>")
    escaped = re.sub(r"&lt;(/?(?:sub|sup|b|i|strong|em))&gt;", r"<\1>", escaped)
    escaped = re.sub(r"\^\{([^{}\n]+)\}", r"<sup>\1</sup>", escaped)
    escaped = re.sub(r"_\{([^{}\n]+)\}", r"<sub>\1</sub>", escaped)
    def explicit(match):
        kind, n, label = match.groups()
        key = kind+"/"+n
        if key not in known:
            raise LayoutError([{"check": "missing-navigation-target", "id": key}])
        return linked(key, label or n)
    escaped = re.sub(r"\[\[(fig|ref|table|eq):(\d+)(?:\|([^\]]+))?\]\]", explicit, escaped)
    # Tokenize to avoid creating nested anchors and to preserve superscript tags.
    parts = re.split(r"(<[^>]+>)", escaped)
    anchor_depth = 0
    def ref_marker(match):
        content = match[1]
        def number(m):
            key = "ref/"+m[0]
            return linked(key, m[0]) if key in known else m[0]
        return "["+re.sub(r"\d+", number, content)+"]"
    def object_marker(match, context):
        prefix, n = match.groups()
        if re.search(r"supplement|supporting|补充", context[max(0, match.start()-25):match.start()], re.I):
            return match[0]
        kind = "fig" if prefix.lower().startswith(("fig", "图")) else "table" if prefix.lower().startswith(("table", "表")) else "eq"
        key = kind+"/"+n
        return linked(key, match[0]) if key in known else match[0]
    for i, part in enumerate(parts):
        if part.startswith("<"):
            if re.match(r"<a\b", part):
                anchor_depth += 1
            elif part.startswith("</a"):
                anchor_depth -= 1
            continue
        if not anchor_depth:
            if i and parts[i-1].lower() == '<sup>' and canonical(part) in citation_tokens:
                parts[i] = re.sub(r'\d+', lambda m: anchor('ref/'+m[0], m[0]) if 'ref/'+m[0] in known else m[0], part)
                continue
            part = re.sub(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]", ref_marker, part)
            # Supplementary objects have their own namespace and are deliberately
            # left unmapped; 'Fig. S1' also cannot match this numeric grammar.
            context = part
            part = re.sub(r"((?:Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?|图|表|式)\s*\(?)(\d+)\)?",
                          lambda m: object_marker(m, context), part, flags=re.I)
            parts[i] = part
    marked = "".join(parts)
    if author_years:
        # Longest first so "Kim et al., 2016" wins over "Kim et al.".
        tokens = sorted(author_years, key=len, reverse=True)
        chunks = re.split(r'(<[^>]+>)', marked)
        for i, chunk in enumerate(chunks):
            if not chunk or chunk.startswith('<'):
                continue
            for token in tokens:
                colour = author_years[token]
                needles = []
                for candidate in (html.escape(token), token):
                    if candidate and candidate not in needles:
                        needles.append(candidate)
                for needle in needles:
                    if needle not in chunk:
                        continue
                    chunk = chunk.replace(
                        needle,
                        f'<span style="color:{colour};">{needle}</span>')
                    break
            chunks[i] = chunk
        marked = ''.join(chunks)
    return marked


def _deduplicate(protected):
    result, seen = [], set()
    for item in protected:
        key = (item["id"], tuple(item["rect"]))
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result

def _prose_order(a,b):
    """Keep paragraph order; only a small raised inline glyph follows its base."""
    first,second=sorted([a,b],key=lambda r:(r['rect'][1],r['rect'][0]))
    size=max(first.get('size',9.35),second.get('size',9.35))
    if (first.get('inline_fragment') and not second.get('inline_fragment')
            and first.get('size',9.35) <= second.get('size',9.35)
            and first['rect'][2]-first['rect'][0] <= second.get('size',9.35)*2
            and first['rect'][3]-first['rect'][1] <= second.get('size',9.35)*1.5
            and second['rect'][1]-first['rect'][1] <= size
            and first['rect'][0] >= second['rect'][0]):
        first,second=second,first
    return first,second


# Compatibility alias for existing callers; keep one scientific formatter.
from .scientific_markup import restore_scientific_markup as scientific_markup


def compile_layout(mapping, fonts=None):
    """Compile without translating, truncating, shrinking, or changing page order.

    Expansion uses only vertical blank space in the source column. A collision
    with a native object fails explicitly. Overlapping prose fragments may share
    a region only in the same column; all contributing IDs are retained.
    """
    if not mapping.get("layout_reviewed"):
        raise LayoutError([{"check": "source-map-not-reviewed"}])
    from .structure import source_order, audit_order
    prepare_render_text(mapping)
    blocks = mapping["blocks"]
    link_blue = dominant_link_blue(
        line for block in blocks for line in (block.get('source_lines') or []))
    rank = {ident: n for n, ident in enumerate(source_order(mapping))}
    ids = [b["id"] for b in blocks]
    if len(ids) != len(set(ids)) or set(ids) != set(mapping.get("expected_block_ids", ids)):
        raise LayoutError([{"check": "source-block-coverage"}])
    bibliography = deepcopy(mapping.get("references", []))
    append_refs = mapping.get("explicitappendrefs") is True
    targets = deepcopy(mapping.get("targets", {}))
    # The legacy writer appends every references entry. An in-place reader has
    # native bibliography destinations instead; keep the directory separately.
    if not append_refs:
        for ref in bibliography:
            key = f"ref/{ref['number']}"
            if key not in targets and ref.get("source_page") and ref.get("rect"):
                targets[key] = {"source_page": ref["source_page"], "point": ref["rect"][:2]}
        missing = [f"ref/{r['number']}" for r in bibliography if f"ref/{r['number']}" not in targets]
        if missing:
            raise LayoutError([{"check": "native-reference-target-missing", "id": key} for key in missing])
    known = set(targets) | ({f"ref/{r['number']}" for r in bibliography} if append_refs else set())
    from .fonts import resolve_fonts
    manifest = {"schema_version": "4.0", "title": mapping.get("title", "中文全文阅读版"),
                "scope": mapping.get("scope", "全文译文，原版式"), "source_sha256": mapping["source_sha256"],
                "fonts": resolve_fonts(fonts or mapping.get("fonts")),
                "targets": targets, "references": bibliography if append_refs else [], "bibliography": bibliography,
                "explicitappendrefs": append_refs,
                "link_blue": link_blue,
                "pages": [], "expected_block_ids": ids}
    for key in ("layout_mode", "requested_layout", "layout_change_authorized", "reference_layout"):
        if key in mapping:
            manifest[key] = mapping[key]
    issues = []
    warnings = []
    page_ids = [p["source_page"] for p in mapping["pages"]]
    if len(page_ids) != len(set(page_ids)) or any(b["source_page"] not in page_ids for b in blocks):
        raise LayoutError([{"check": "source-page-coverage"}])
    for page in mapping["pages"]:
        pn = page["source_page"]
        selected = [b for b in blocks if b["source_page"] == pn]
        bounds = page.get("rect")
        if not bounds:
            raise LayoutError([{"check": "missing-page-bounds", "page": pn}])
        protected = deepcopy(page.get("protected", []))
        erase_guards = []
        regions = []
        for b in selected:
            box = b["rect"]
            if len(box) != 4 or box[0] >= box[2] or box[1] >= box[3] or box[0] < bounds[0] or box[1] < bounds[1] or box[2] > bounds[2] or box[3] > bounds[3]:
                issues.append({"check": "invalid-source-rectangle", "page": pn, "id": b["id"]})
            if b.get("absorbed"):
                continue
            if not b.get("translation_required", True):
                if not b.get("preservation_reason"):
                    issues.append({"check": "missing-preservation-reason", "id": b["id"]})
                protected.append({"id": b["id"], "rect": list(box), "type": b.get("type", "native")})
                # Preserve original per-span font bounds, not the enclosing
                # source block (which can include translated neighboring text).
                for line in b.get('source_lines', []):
                    for span in line.get('spans', []):
                        if span.get('text', '').strip() and all(k in span for k in ('bbox', 'font', 'origin')):
                            erase_guards.append({'id': b['id'], 'rect': list(span['bbox']),
                                                 'font': span['font'], 'origin': list(span['origin']),
                                                 'text': span['text']})
                continue
            text = b.get("render_html", b.get("translation", ""))
            if not isinstance(text, str) or not text.strip():
                issues.append({"check": "missing-translation", "id": b["id"]})
                continue
            source_lines = b.get('source_lines', [])
            # Continuation slices share the anchor's citation geometry so a
            # superscript evidenced on the first box can still be restored.
            if b.get("translation_in"):
                anchor = next((a for a in selected if a["id"] == b["translation_in"]), None)
                if anchor and not source_lines:
                    source_lines = anchor.get("source_lines", [])
            text, citation_tokens, colours, citation_issues = restore_citations(text, source_lines, known)
            for issue in citation_issues:
                tagged = {**issue, 'id': b['id'], 'page': pn}
                if issue.get('check') == 'citation-alignment-needs-review':
                    warnings.append({**tagged, 'severity': 'warning'})
                else:
                    issues.append(tagged)
            colours = {**object_colours(source_lines, known), **colours}
            ay = author_year_colours(source_lines)
            cap_key, cap_ink = caption_title_ink(source_lines) if b.get('type') == 'caption' else (None, None)
            markup = _markup(scientific_markup(text, source_lines), known, colours, citation_tokens, ay,
                             link_blue=link_blue, caption_key=cap_key, caption_ink=cap_ink)
            html = "<b>"+markup+"</b>" if b.get("bold") else markup
            absorbed_ids = list(b.get("absorbs") or [])
            source_ids = sorted({b["id"], *absorbed_ids}, key=lambda i: rank.get(i, len(rank)))
            effective_box = union([box, *(b.get("absorb_rects") or [])]) if b.get("absorb_rects") else list(box)
            region = {"id": b["id"], "source_block_ids": source_ids, "rect": list(effective_box),
                      "block_type": b.get("type", "paragraph"),
                      "source_rects": [list(box), *[list(r) for r in b.get("absorb_rects") or []]], "html": html,
                      "paragraph_start": b.get("paragraph_start", True),
                      "inline_fragment": b.get("role") == "inline-fragment",
                      **{k: b[k] for k in ("size", "leading", "min_scale", "source_font_pt", "logical_paragraph_id") if k in b and b[k] is not None}}
            region["parts"] = [{"rank": rank.get(b["id"], len(rank)), "html": html,
                                "paragraph_start": region["paragraph_start"],
                                "inline_fragment": region["inline_fragment"]}]
            regions.append(region)
        protected = _deduplicate(protected)
        def leading_bleed(a, b):
            # Dense table rows share font-metric boxes by a point or two; that
            # is leading bleed, not a layout conflict or a merge trigger.
            vertical = min(a[3], b[3]) - max(a[1], b[1])
            return 0 < vertical <= max(2.5, .35 * max(1.0, min(a[3]-a[1], b[3]-b[1], 20)))
        def contains_rect(outer, inner):
            return (outer[0] <= inner[0]+1 and outer[1] <= inner[1]+1
                    and outer[2] >= inner[2]-1 and outer[3] >= inner[3]-1)
        groups = column_groups([r["rect"] for r in regions], bounds)
        for r, group in zip(regions, groups):
            r["column"] = group
        # Union overlapping prose, including superscript/chemical fragments, but
        # never bridge a gutter. Connected components are resolved transitively.
        changed = True
        while changed:
            changed = False
            for i, a in enumerate(regions):
                for j in range(i+1, len(regions)):
                    b = regions[j]
                    if not intersects(a["rect"], b["rect"]) or leading_bleed(a["rect"], b["rect"]):
                        continue
                    same_column = a["column"] == b["column"] and a["column"] != "full"
                    nested_fragment = ((a["rect"][0] <= b["rect"][0]+1 and a["rect"][2] >= b["rect"][2]-1)
                                       or (b["rect"][0] <= a["rect"][0]+1 and b["rect"][2] >= a["rect"][2]-1))
                    # A short fragment geometrically inside another block (a
                    # caption tail such as `2).`) follows its parent; containment
                    # defines reading order regardless of column labels.
                    contained = contains_rect(a["rect"], b["rect"]) or contains_rect(b["rect"], a["rect"])
                    if not same_column and not contained and not (a["column"] == b["column"] == "full" and nested_fragment):
                        issues.append({"check": "cross-column-prose-overlap", "id": a["id"], "other": b["id"], "page": pn})
                        continue
                    first, second = _prose_order(a,b)
                    # Overlapping extraction boxes can invert a fragment and
                    # its parent; the compiled text order must follow the same
                    # source order the reading-order audit verifies.
                    parts = first.get("parts", []) + second.get("parts", [])
                    parts.sort(key=lambda p: p["rank"])
                    html = ""
                    for index, part in enumerate(parts):
                        if index and part["paragraph_start"] and not part["inline_fragment"]:
                            html += "<br/>"
                        html += part["html"]
                    merged = {**first, "id": first["id"]+"+"+second["id"], "rect": union([a["rect"], b["rect"]]),
                              "source_block_ids": sorted(first["source_block_ids"]+second["source_block_ids"],
                                                         key=lambda i: rank.get(i, len(rank))),
                              "source_rects": first["source_rects"]+second["source_rects"],
                              "html": html, "parts": parts,
                              "size": min(a.get("size", 9.35), b.get("size", 9.35)),
                              "leading": min(a.get("leading", 1.17), b.get("leading", 1.17)),
                              "min_scale": max(a.get("min_scale", .88), b.get("min_scale", .88))}
                    if a.get("logical_paragraph_id") and a.get("logical_paragraph_id") == b.get("logical_paragraph_id"):
                        merged["logical_paragraph_id"] = a["logical_paragraph_id"]
                    else:
                        merged.pop("logical_paragraph_id", None)
                    regions[i] = merged
                    regions.pop(j)
                    changed = True
                    break
                if changed:
                    break
        # White-space allocation stops at every horizontal obstacle, irrespective
        # of its column label. Neither redactions nor overlays consume that gap.
        for region in regions:
            box = region["rect"]
            for native in protected:
                # Font-metric bleed is tolerated only between text-like native
                # objects; a paragraph overlapping a figure is always damage.
                text_native = native.get("type") not in ("figure",)
                if intersects(box, native["rect"]) and not (text_native and leading_bleed(box, native["rect"])):
                    issues.append({"check": "protected-intersection", "page": pn, "id": region["id"], "protected_id": native["id"]})
            inline_obstacle = lambda o: contains_rect(box, o) and (o[2]-o[0]) <= 16 and (o[3]-o[1]) <= 16
            blockers = ([r["rect"] for r in regions if r is not region]+[p["rect"] for p in protected]
                        +[o for o in page.get("spacing_obstacles", []) if not inline_obstacle(o)])
            # Any horizontally overlapping object that starts below this region's
            # top limits the downward growth — including a neighbor whose top
            # sits a point above the region bottom because of font-metric bleed.
            below = [b[1] for b in blockers if min(box[2], b[2]) > max(box[0], b[0]) and b[1] > box[1] + .5]
            limit = min(below+[bounds[3]-max(12, bounds[3]*.025)])
            # Keep a 1pt visual separation and never extend past a native
            # object. Shrinking a row bottom that bleeds into the next line is
            # also required: the redaction and text clip must not capture the
            # neighbour's glyphs.
            if limit-1 > box[1]+2 and abs((limit-1)-box[3]) > .01:
                region["rect"][3] = limit-1
            for native in protected:
                text_native = native.get("type") not in ("figure",)
                if intersects(region["rect"], native["rect"]) and not (text_native and leading_bleed(region["rect"], native["rect"])):
                    issues.append({"check": "protected-intersection-after-spacing", "page": pn, "id": region["id"], "protected_id": native["id"]})
        original_masks = [box for r in regions for box in r['source_rects']]
        manifest["pages"].append({"source_page": pn, "regions": regions,
                                  "redactions": split_masks(original_masks, erase_guards),
                                  "redactions_original": original_masks,
                                  "erase_guards": erase_guards, "protected": protected})
    # Region list order matters when fragments share a textbox and when a reader
    # traverses the generated text layer. Derive it from source column bands.
    for page in manifest['pages']:
        page['regions'].sort(key=lambda r: min(rank[i] for i in r['source_block_ids']))
    issues.extend(audit_order(mapping, manifest)['issues'])
    manifest['warnings'] = warnings
    if issues:
        raise LayoutError(issues)
    return manifest
