"""Geometry-aware scientific text classification into legacy source maps.

All source objects remain accounted for. Heuristics are reviewable suggestions,
not claims of semantic understanding or certification of a paper's completeness.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import re

from .pdf_reader import block_text, records_from_pages, script_markup, stable_id, visible_line_bbox
from .stacked_selectivity import (
    apply_pua_digits, is_stacked_script_fragment, same_visible_chars, synthesize_stacked_selectivity)

CAPTION = re.compile(r"^(?P<kind>Fig(?:ure)?\.?|Table)\s*(?P<number>\d+)\s*(?:[.:|]|[–—-])\s+", re.I)
REF_HEAD = re.compile(r"^(?:references(?: and notes)?|bibliography)\s*[.:]?\s*$", re.I)
REF_START = re.compile(r"^\s*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})\.)\s+")
POST_REF = re.compile(r"^(?:acknowledg\w*|author contributions|authors?[\x27\u2019]?\s*(?:declared\s+)?contributions?|author information|information about (?:the )?authors?|funding|competing interests|conflicts? of interest|declaration\w*|supporting information|supplementary information|data availability|code availability|additional information|associated content|notes)\b", re.I)
MATH = re.compile(r"[=≈≠≤≥∑∫√×±−→⇌⇋↔⇄⟶\x80-\x9f]")
META = re.compile(r"\b(?:university|institute|department|laboratory|college|correspondence|e-?mail|orcid|received|accepted|published online)\b|@", re.I)
UNIT = re.compile(r"^(?:[-+−]?\d[\d.,]*\s*)?(?:[μµmnpk]?m|[μµmnpk]?g|s|K|°C|eV|keV|mol|mmol|M|mM|Hz|kHz|MPa|Pa|%)(?:[−-]?\d+|[²³])?(?:\s*[/·]\s*\w+(?:[−-]?\d+|[²³])?)*$")
MICRO_EQ_NUMBER = re.compile(r"^\(\d{1,3}\)[.,;:]?$")
MICRO_NATIVE_CHARS = re.compile(r"^[A-Za-z0-9()\[\]{}+\-−·.,;:/α-ωΑ-Ω\x80-\x9f]+$")
MICRO_STOPWORDS = {'a', 'an', 'the', 'we', 'it', 'is', 'in', 'on', 'of', 'to', 'and',
                   'or', 'as', 'at', 'by', 'for', 'from', 'with', 'this', 'that', 'no'}


def native_micro_fragment(text, rect):
    """Tiny equation numbers, single formulas and reference tails stay native.

    These blocks are too small for translated text and carry no sentence; they
    must not consume a translation slot or trigger full-width punctuation
    overflow in the builder. All-caps section headings (``1. INTRODUCTION``,
    ``HIGHLIGHTS``, ``KEYWORDS``) are words, not formulas.
    """
    if rect[2]-rect[0] > 120 or rect[3]-rect[1] > 16:
        return False
    compact = re.sub(r'\s+', '', re.sub(r'<[^>]+>', '', text))
    if not compact or len(compact) > 20 or compact.lower() in MICRO_STOPWORDS:
        return False
    if MICRO_EQ_NUMBER.fullmatch(compact):
        return True
    if not MICRO_NATIVE_CHARS.fullmatch(compact):
        return False
    # Two lowercase letters in a row means a word, not a formula.
    if re.search(r'[a-z]{2,}', compact):
        return False
    # Three capitals in a row, or a numbered section label, is an all-caps
    # heading (INTRODUCTION, EXPERIMENTAL, RESULTS, HIGHLIGHTS, KEYWORDS).
    if re.search(r'[A-Z]{3,}', compact):
        return False
    if re.match(r'\d+[.)]', compact) and re.search(r'[A-Za-z]{3,}', compact):
        return False
    # A bare number or `[37].` is a data value/citation, not a formula; keep it
    # in the translation pipeline. Native micro content carries an element or
    # formula letter.
    return bool(re.search(r'[A-Za-z]', compact))


def adapted_body_size(source_pt):
    """Body size follows the source, clamped to the reading range [8.5, 9.5] pt."""
    try:
        source_pt = float(source_pt)
    except (TypeError, ValueError):
        source_pt = 9
    if source_pt < 7.8:
        return round(source_pt, 1)
    return min(9.5, max(8.5, round(source_pt * 1.05, 1)))


# Rendered Chinese body/abstract text cannot shrink below this. The public
# reading style already uses 9 pt for abstracts and 9.5 pt for body; 9 pt is
# the floor so adaptive fit cannot crush a paragraph to the ~5 pt superscript
# size that a merged selectivity charge used to impose. Captions and tiny
# inline citation chips are outside this floor.
BODY_FONT_FLOOR = 9.0


def body_font_floor_applies(block):
    """True for a real body/abstract region, not a caption, chip, or script glyph."""
    kind = block.get('block_type') or block.get('type')
    if kind not in {'paragraph', 'abstract'}:
        return False
    text = block.get('source_text') or block.get('html') or ''
    if is_stacked_script_fragment(text):
        return False
    rect = block.get('rect') or [0, 0, 0, 0]
    try:
        # Citation chips and stacked-charge glyphs are smaller than a text line
        # in a column. The floor is for column body copy, which is wider.
        tiny = (float(rect[2]) - float(rect[0])) <= 80 and (float(rect[3]) - float(rect[1])) <= 30
    except (TypeError, ValueError, IndexError):
        tiny = False
    if tiny:
        return False
    return True


def clamp_body_font(size, block):
    """Raise a body size to ``BODY_FONT_FLOOR`` when the floor applies."""
    try:
        size = float(size)
    except (TypeError, ValueError):
        size = BODY_FONT_FLOOR
    if body_font_floor_applies(block):
        return max(BODY_FONT_FLOOR, size)
    return size


def normalize_heading(text):
    text = re.sub(r"^[\s■●•▪◼◆*]+", "", text).strip().rstrip(".:").lower()
    compact = re.sub(r"\s+", "", text)
    if compact in {"abstract", "references", "bibliography", "referencesandnotes"}:
        return "references and notes" if compact == "referencesandnotes" else compact
    return re.sub(r"\s+", " ", text)


def rule_tables(page):
    """Three horizontal rules plus a nearby standalone Table label define a clip.

    No whole-page text-strategy table detection: separate tables retain separate
    boundaries, and unrelated plot grids lack the required caption grammar.
    """
    captions = [b["bbox"] for b in page.get("blocks", [])
                if re.match(r"^Table\s+\d+(?:\s*\n|\s*[.:|])", block_text(b), re.I)]
    groups = []
    for box in sorted(page.get("drawing_rules", []), key=lambda b: (b[0], b[2], b[1])):
        group = next((g for g in groups if abs(g[0][0]-box[0]) <= 2 and abs(g[0][2]-box[2]) <= 2), None)
        if group is None:
            groups.append([box])
        else:
            group.append(box)
    found = []
    for group in groups:
        ordered = sorted(group, key=lambda b: b[1])
        for caption in captions:
            starts = [i for i, b in enumerate(ordered) if 0 <= b[1]-caption[3] <= 50
                      and min(b[2], caption[2])-max(b[0], caption[0]) >= .5*min(b[2]-b[0], caption[2]-caption[0])]
            if not starts:
                continue
            start = starts[0]
            candidates = []
            for box in ordered[start:]:
                if candidates and box[1]-candidates[-1][3] > 100:
                    break
                if box[1]-ordered[start][1] > 300:
                    break
                candidates.append(box)
            if len(candidates) >= 3:
                # Caption below the top three rules of another table terminates
                # this candidate before its own top rule.
                later = [c[1] for c in captions if c[1] > caption[3] and c[1] > candidates[0][1]]
                limit = min(later) if later else float("inf")
                candidates = [b for b in candidates if b[3] < limit]
                if len(candidates) >= 3:
                    found.append(union(candidates))
    return found


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def contains(a, b, tolerance=1):
    return a[0]-tolerance <= b[0] and a[1]-tolerance <= b[1] and a[2]+tolerance >= b[2] and a[3]+tolerance >= b[3]


def dominant(lines):
    weights = Counter()
    for line in lines:
        for span in line.get("spans", []):
            weights[round(span.get("size", 9.35), 1)] += len(span.get("text", "").strip())
    return weights.most_common(1)[0][0] if weights else 9.35


def column_groups(boxes, bounds):
    """Shared left/right column grouping for classification and layout.

    A split needs narrow blocks on both sides of a real gutter; full-width blocks
    are independent anchors. No journal-specific coordinates are assumed.
    """
    width = bounds[2]-bounds[0]
    mid = (bounds[0]+bounds[2])/2
    # Tiny equation numbers, labels and superscript fragments near the gutter
    # cannot establish (or collapse) the prose columns.
    left = [b for b in boxes if b[2] <= mid+width*.035 and width*.18 <= b[2]-b[0] < width*.57]
    right = [b for b in boxes if b[0] >= mid-width*.035 and width*.18 <= b[2]-b[0] < width*.57]
    split = bool(left and right and min(b[0] for b in right)-max(b[2] for b in left) >= width*.005)
    if not split:
        return ["single"]*len(boxes)
    gutter = (max(b[2] for b in left)+min(b[0] for b in right))/2
    return ["left" if b[2] <= gutter else "right" if b[0] >= gutter else "full" for b in boxes]


def reading_order(items, bounds):
    groups = column_groups([i["rect"] for i in items], bounds)
    for item, group in zip(items, groups):
        item["column"] = group
    # MinerU reading order is the structural authority for blocks it claimed.
    # Unclaimed leftovers sit between the claimed neighbours by vertical position.
    if any("mineru_reading_index" in item for item in items):
        claimed = [item for item in items if "mineru_reading_index" in item]
        def mineru_key(item):
            if "mineru_reading_index" in item:
                return (item["mineru_reading_index"], 0, item["id"])
            y = item["rect"][1]
            before = [c["mineru_reading_index"] for c in claimed if c["rect"][1] <= y]
            after = [c["mineru_reading_index"] for c in claimed if c["rect"][1] > y]
            if before and after:
                slot = (max(before) + min(after)) / 2
            elif before:
                slot = max(before) + 0.5
            elif after:
                slot = min(after) - 0.5
            else:
                slot = 0
            return (slot, 1, item["id"])
        return sorted(items, key=mineru_key)
    anchors = sorted((i for i in items if i["column"] == "full"), key=lambda i: (i["rect"][1], i["rect"][0], i["id"]))
    def key(item):
        b = item["rect"]
        band = sum(a["rect"][3] <= b[1] for a in anchors)
        # Full-width anchors end the preceding band, so column text above them
        # is read first and text below them starts a new column band.
        return (band, {"left": 0, "single": 0, "right": 1, "full": 2}[item["column"]], b[1], b[0], item["id"])
    return sorted(items, key=key)


def _formula(text):
    words = re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", text)
    visible = sum(not c.isspace() for c in text)
    # Scientific headings and connecting prose remain translatable even if an
    # inline equation leaves few long words in this one extracted line.
    if re.search(r'\b(?:then|where|thus|hence|with|from|given|using)\b',text,re.I):return False
    # Prose line wraps that end in an equation reference (`...expressed by` /
    # `eq 9:`, `by eq 10:`) are short and numeric, but they are sentence
    # fragments, not displayed equations. Translate them as prose.
    if re.search(r'\b(?:eq(?:uation|n)?)\.?\s*\d+\s*:?\s*$', text, re.I):return False
    if len(words)>=3 and text.upper()==text and not MATH.search(text):return False
    # Greek letters name sections and concepts too. A Greek glyph alone is not
    # evidence of a displayed equation; require mathematical operators/structure.
    reaction = bool(re.search(r"[→⇌↔⇄⟶]", text) and not re.search(r"\b[a-zа-яё]{4,}\b", text))
    return bool(text and (reaction or (MATH.search(text) and len(words) < 3)
                         or re.fullmatch(r"\(\d{1,3}\)", text)
                         or (visible and any(c.isdigit() for c in text)
                             and not re.search(r"[A-Za-zА-Яа-яЁё]{3}|[α-ωΑ-Ω]", text) and not UNIT.fullmatch(text))))


def _near(box, objects, size):
    for obj in objects:
        horizontal = min(box[2], obj[2])-max(box[0], obj[0])
        gap = max(obj[1]-box[3], box[1]-obj[3], 0)
        # Plot bounds stop before tick/axis text and panel letters. Their caption
        # may begin another line below those labels, including a full-width
        # caption spanning several neighboring panels.
        if horizontal >= .3*min(box[2]-box[0], obj[2]-obj[0]) and gap <= max(48, size*6):
            return True
    return False


def _line_groups(record, tables, prose, obstacles=(), running=(), bounds=None):
    """Split only at native table/formula boundaries, preserving inline fragments."""
    lines = record.get("lines", [])
    if not lines:
        return [("paragraph", record["bbox"], record["text"], [])]
    sibling_prose = [visible_line_bbox(l2, record["bbox"]) for l2 in lines
                     if len(re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", block_text({"lines": [l2]}))) >= 2]
    kinds = []
    for line in lines:
        text = block_text({"lines": [line]})
        heading = normalize_heading(text)
        box = visible_line_bbox(line, record["bbox"])
        h = (bounds[3] - bounds[1]) if bounds else 0
        line_boundary = bool(bounds and (box[3] < bounds[1] + h * .105 or box[1] > bounds[3] - h * .08))
        rkey = re.sub(r'\d+', '#', text).strip().lower()
        if ( line_boundary or bounds is None ) and rkey in running and rkey != '#':
            kinds.append('header' if (bounds and box[3] < bounds[1] + h * .105) else 'footer')
            continue
        if REF_HEAD.fullmatch(heading):
            kinds.append("reference-heading")
            continue
        if heading == "abstract":
            kinds.append("abstract")
            continue
        if len(text) < 100 and POST_REF.match(heading):
            kinds.append("post-reference-heading")
            continue
        # Table cells sometimes have multiple spans, sometimes separate lines at
        # the same baseline. Geometry detection is stronger evidence than digits.
        in_table = any(contains(t, box, 2) for t in tables)
        spans = line.get("spans", [])
        spaced_cells = sum(spans[i+1]["bbox"][0]-spans[i]["bbox"][2] > max(12, dominant([line])*1.5)
                           for i in range(len(spans)-1) if "bbox" in spans[i] and "bbox" in spans[i+1]) >= 1
        numeric_row = len(re.findall(r"(?<!\w)[-+]?\d+(?:\.\d+)?", text)) >= 3 and len(re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", text)) <= 2
        inline = any(intersects(box, p) for p in prose if p != record.get("tight_bbox", record["bbox"]))
        # PDF justification often emits "of", "is", or a numeric/unit phrase as
        # separate blocks on the same prose baseline. Adjacency still means an
        # inline fragment, even when the extraction rectangles do not intersect.
        inline = inline or any(abs((box[1]+box[3])-(p[1]+p[3])) <= dominant([line])*1.3
                               and max(p[0]-box[2], box[0]-p[2], 0) <= dominant([line])*5
                               for p in prose if p != record.get("tight_bbox", record["bbox"]))
        inline = inline or any(sbox != box and (intersects(box, sbox) or (
                               abs((box[1]+box[3])-(sbox[1]+sbox[3])) <= dominant([line])*1.3
                               and max(sbox[0]-box[2], box[0]-sbox[2], 0) <= dominant([line])*5))
                               for sbox in sibling_prose)
        kinds.append("table" if in_table or spaced_cells and numeric_row else
                     "formula" if _formula(text) and not inline else "paragraph")
    # A bare equation number adjacent to prose is ambiguous; do not create a
    # protected cutout in the same rectangle as its translated paragraph.
    groups = []
    for kind, line in zip(kinds, lines):
        box=visible_line_bbox(line,record['bbox'])
        previous=union([visible_line_bbox(l,record['bbox']) for l in groups[-1][3]]) if groups else None
        wrap_split=bool(previous and any(intersects(union([previous,box]),obj)
                         and not intersects(previous,obj) and not intersects(box,obj) for obj in obstacles))
        # A large decorative section bullet has inflated font metrics. Preserve
        # its own original span and translate only the separately bounded label.
        spans=line.get('spans',[])
        markers=[s for s in spans if s.get('text','').strip() in {'■','●','•','▪','◼','◆'}]
        normal=[s for s in spans if s not in markers and s.get('text','').strip()]
        if markers and normal and kind in {'reference-heading','post-reference-heading','paragraph'}:
            groups.append(['formula',None,None,[{**line,'spans':markers}]])
            groups.append([kind,None,None,[{**line,'spans':normal}]])
        elif groups and groups[-1][0] == kind and kind not in {"header", "footer"} and not wrap_split:
            groups[-1][3].append(line)
        else:
            groups.append([kind, None, None, [line]])
    return [(k, union([visible_line_bbox(l, record["bbox"]) for l in ls
                       if block_text({"lines": [l]})] or [record["bbox"]]), block_text({"lines": ls}), ls)
            for k, _, _, ls in groups]


def _stacked_formula_entry(record, pn):
    """One native math block. Stacked markup is kept only when it is the same glyphs."""
    raw = record.get('text') or ''
    markup = record.get('stacked_markup') or ''
    text = markup if markup and same_visible_chars(markup, raw) else raw
    box = list(record.get('tight_bbox') or record['bbox'])
    lines = record.get('lines') or []
    size = dominant(lines) if lines else 9
    entry = {"id": record['id'], "source_page": pn, "rect": box, "source_text": text,
             "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "type": "formula", "translation_required": False, "translation": "",
             "preservation_reason": "native-stacked-selectivity",
             "stacked_markup": text, "size": size, "source_font_pt": size,
             "leading": 1.17, "min_scale": 1, "original_ids": [record['id']],
             "original_bbox": list(record['bbox']),
             "source_lines": [{"text": block_text({"lines": [line]}),
                               "bbox": visible_line_bbox(line, box),
                               "original_bbox": list(line.get("bbox", box)),
                               "spans": deepcopy(line.get("spans", []))} for line in lines]}
    return entry


def classify(inspection):
    """Produce a source_map-compatible suggestion with explicit review issues."""
    pages = inspection["pages"]
    for page in pages:
        # Repeat the extract-time pass so a caller that built blocks by hand,
        # and a page already synthesized in parse(), both end in one math block.
        apply_pua_digits(page.get("blocks") or [])
        synthesize_stacked_selectivity(page.get("blocks") or [])
    records, originals, identity_issues = records_from_pages(pages)
    # Recompute from legacy blocks: insert/filter/reorder operations must never
    # make an old records array silently override the actual inspection.
    issues = deepcopy(inspection.get("issues", []))+identity_issues
    result = {"schema_version": "4.0", "source_sha256": inspection["sha256"],
              "layout_reviewed": False, "terminology_ledger": [], "blocks": [], "pages": [],
              "targets": {}, "references": [], "reference_candidates": [],
              "id_to_original": originals, "issues": issues}
    body = dominant([line for r in records for line in r.get("lines", [])])
    running = Counter()
    for page in pages:
        h = page["rect"][3]-page["rect"][1]
        page_running = {re.sub(r"\d+", "#", r["text"]).strip().lower()
                        for r in records if r["page"] == page["source_page"] and r["text"]
                        and (r["bbox"][3] < page["rect"][1]+h*.105 or r["bbox"][1] > page["rect"][3]-h*.08)}
        page_running.update({re.sub(r'\d+', '#', block_text({'lines':[l]})).strip().lower()
                        for r in records if r['page']==page['source_page'] for l in r.get('lines',[])
                        if l['bbox'][3] < page['rect'][1]+h*.105 or l['bbox'][1] > page['rect'][3]-h*.08})
        running.update(page_running)
    repeated = {k for k, n in running.items() if k != "#" and n >= 2 and n >= len(pages)*.5}
    in_refs = False
    seen_ref_head = False
    refs_by_number = {}
    last_ref = None
    reference_style = None
    for page in pages:
        pn, bounds = page["source_page"], page["rect"]
        source = sorted((r for r in records if r["page"] == pn), key=lambda r: (r["bbox"][1], r["bbox"][0], r["id"]))
        page_body = dominant([line for r in source for line in r.get("lines", [])])
        # Require a numbered display AND specialized math glyphs AND an
        # operator nearby. Italic fonts alone never turn prose into a formula.
        equation_zones = []
        math_glyphs = [s['bbox'] for r in source for l in r.get('lines',[]) for s in l.get('spans',[])
                       if 'math' in s.get('font','').lower() and s.get('text','').strip()]
        for r in source:
            if not re.search(r'(?:^|\n)\s*\(\d{1,3}\)\s*$', r['text'].strip()): continue
            number = r['bbox']
            seeds = [b for b in math_glyphs if 0 <= number[0]-b[2] <= body*30
                     and max(b[1]-number[3], number[1]-b[3], 0) <= body*1.5]
            if not seeds: continue
            zone=union(seeds+[number]);zone=[zone[0]-body*3,zone[1]-1,zone[2]+1,zone[3]+1]
            if zone[3]-zone[1] > body*6: continue
            if any(contains(zone, visible_line_bbox(l,r2['bbox'])) and MATH.search(block_text({'lines':[l]}))
                   for r2 in source for l in r2.get('lines',[])):
                equation_zones.append(zone)
        source_columns = dict(zip((r["id"] for r in source), column_groups([r.get("tight_bbox", r["bbox"]) for r in source], bounds)))
        images = [r["bbox"] for r in source if r["type"] == "figure"]
        decorative_ink=[s['nonwhitespace_bbox'] for r in source for l in r.get('lines',[]) for s in l.get('spans',[])
                        if s.get('native_ink_proof') and s.get('nonwhitespace_bbox')]
        tables = [list(t["bbox"]) for t in page.get("tables", [])]+rule_tables(page)
        # Some PDF detectors see only the ruled header. A caption plus an aligned
        # closing rule provides independent evidence of the full native table.
        table_captions=[r for r in source if re.match(r'^Table\s*\d+[.:]',r['text'],re.I)]
        # Native table cells can be short prose-like words (`membrane`, SYMA-1)
        # that no math or digit heuristic covers. Caption proximity plus a short,
        # sentence-free block is table evidence: cells sit directly below their
        # caption, prose paragraphs do not.
        table_caption_zones = [r["bbox"] for r in table_captions]
        for table in tables:
            if not any(0<=table[1]-r['bbox'][3]<=80 and min(table[2],r['bbox'][2])>max(table[0],r['bbox'][0]) for r in table_captions):continue
            closing=[r[1] for r in page.get('drawing_rules',[]) if abs(r[0]-table[0])<3 and abs(r[2]-table[2])<3
                     and 0<r[1]-table[3]<200]
            if closing:table[3]=min(closing)
        prose = [r.get("tight_bbox", r["bbox"]) for r in source if len(re.findall(r"[A-Za-z]{4,}", r["text"])) >= 4]
        long_prose = [r.get("tight_bbox", r["bbox"]) for r in source
                      if len(r["text"]) >= 100 and len(re.findall(r"[A-Za-z]+", r["text"])) >= 12
                      and re.search(r"\b[a-z]{4,}\b", r["text"])
                      and not re.match(r"^(?:Fig(?:ure)?\.?|Table)\s*\d+", r["text"], re.I)]
        abstract_tops = [r["bbox"][1] for r in source if any(normalize_heading(block_text({"lines": [l]})) == "abstract" for l in r.get("lines", []))
                         or re.match(r"^abstract\b", r["text"], re.I)]
        metadata_limit = min(abstract_tops) if abstract_tops else bounds[1]+(bounds[3]-bounds[1])*.3
        caption_boxes = [r.get("tight_bbox", r["bbox"]) for r in source
                         if len(r["text"]) >= 80 and re.search(r"\b[a-z]{4,}\b", r["text"])
                         and re.match(r"^(?:Fig(?:ure)?\.?|Table)\s*\d+", r["text"], re.I)]
        vectors = [b for b in page.get("drawing_clusters", [])
                   if b[2]-b[0] >= 30 and b[3]-b[1] >= 30
                   and (b[2]-b[0])*(b[3]-b[1]) < .6*(bounds[2]-bounds[0])*(bounds[3]-bounds[1])
                   and not any(contains(b, p) for p in long_prose + caption_boxes)
                   and not any(contains(t, b) or contains(b, t) for t in tables)]
        prose_lines = []
        for r in source:
            full_prose = r.get("tight_bbox", r["bbox"]) in long_prose
            for l in r.get("lines", []):
                value, line_box = block_text({"lines": [l]}), visible_line_bbox(l, r["bbox"])
                if full_prose and len(value) >= 25:
                    prose_lines.append((r["id"], line_box))
        margin = max(40, body*5)
        label_zones = [[max(bounds[0], b[0]-margin), max(bounds[1], b[1]-margin),
                        min(bounds[2], b[2]+margin), min(bounds[3], b[3]+margin)] for b in images+vectors]
        # A purity threshold beside a product name (`≥99.0%`) matches the math
        # glyph set but is a unit value, not a displayed equation. Treating it
        # as an equation seed lets absorption chains swallow the whole
        # materials list.
        threshold = re.compile(r'^[≥≤><]?\s*\d+(?:\.\d+)?\s*%?$')
        math_seeds = [visible_line_bbox(l, r["bbox"]) for r in source for l in r.get("lines", [])
                      if MATH.search(block_text({"lines": [l]}))
                      and not re.search(r"\b[a-z]{4,}\b", block_text({"lines": [l]}))
                      and not threshold.fullmatch(block_text({"lines": [l]}).strip())]
        math_zones = [[b[0]-body*2, b[1]-body*2, b[2]+body*2, b[3]+body*2] for b in math_seeds]
        fragments = [r.get("tight_bbox", r["bbox"]) for r in source if r["text"] and len(r["text"]) <= 80
                     and not re.search(r"\b[a-z]{4,}\b", r["text"])
                     and not any(intersects(r.get("tight_bbox", r["bbox"]), p) for p in long_prose)]
        # Short blocks with real words can still be the left operand of a
        # displayed equation (`water uptake (%)` = ...). Absorb them on the same
        # baseline when they do not end a sentence, so the whole equation stays
        # native instead of half-translated. Connecting prose never absorbs;
        # neither do variable-definition lines (`O is the osmotic flux...`),
        # which carry copulas and articles rather than operand names.
        named_blocks = [r.get("tight_bbox", r["bbox"]) for r in source if r["text"] and len(r["text"]) <= 80
                        and re.search(r"[a-z]", r["text"])
                        and not re.search(r"[.!?]\s*$", r["text"].strip())
                        and not re.search(r'\b(?:then|where|thus|hence|with|from|given|using)\b', r["text"], re.I)
                        and len(re.findall(r'\b(?:is|are|was|were|be|the|of|and|for|in)\b', r["text"], re.I)) < 2
                        # An equation operand name is a short common-noun phrase
                        # (`water uptake (%)`). Supplier/company lists and author
                        # lines are proper-noun chains and never operands.
                        and len(re.findall(r"[A-Za-z]{2,}", r["text"])) <= 8
                        and len(re.findall(r"\b[A-Z][a-z]+\b", r["text"])) <= 2
                        and not META.search(r["text"])
                        and not any(intersects(r.get("tight_bbox", r["bbox"]), p) for p in long_prose)]
        named_absorbed = set()
        # A chain of adjacent fragments can otherwise creep across a dense page
        # and swallow whole prose blocks into one equation zone. A displayed
        # equation spans only a few lines, so cap zone growth at ~10 body lines.
        zone_max_height = body * 10
        for i, seed in enumerate(math_seeds):
            expanded = list(seed)
            changed = True
            included = set()
            fragment_absorbed = set()
            while changed:
                changed = False
                for j, fragment in enumerate(fragments):
                    if j in included:
                        continue
                    gap_x = max(fragment[0]-expanded[2], expanded[0]-fragment[2], 0)
                    gap_y = max(fragment[1]-expanded[3], expanded[1]-fragment[3], 0)
                    if gap_x <= body*3 and gap_y <= body*1.5:
                        candidate = union([expanded, fragment])
                        if candidate[3]-candidate[1] > max(zone_max_height, expanded[3]-expanded[1]):
                            continue
                        expanded = candidate
                        included.add(j)
                        fragment_absorbed.add(tuple(round(v, 1) for v in fragment))
                        changed = True
                for j, block in enumerate(named_blocks):
                    if ('named', j) in included:
                        continue
                    # Skip only the exact block already absorbed as a bare
                    # fragment. The chained union of several fragments can grow
                    # far beyond one equation line and must not swallow a named
                    # operand it merely overlaps — that operand would otherwise
                    # stay prose and collide with the protected equation.
                    if tuple(round(v, 1) for v in block) in fragment_absorbed:
                        continue
                    gap_x = max(block[0]-expanded[2], expanded[0]-block[2], 0)
                    gap_y = max(block[1]-expanded[3], expanded[1]-block[3], 0)
                    # Same baseline means the boxes overlap vertically; a small
                    # tolerance covers extraction jitter. A purely diagonal
                    # near-miss (e.g. a materials list left of a right-column
                    # equation fragment) must not be absorbed.
                    if gap_x <= body*3 and gap_y <= body*.3:
                        candidate = union([expanded, block])
                        if candidate[3]-candidate[1] > max(zone_max_height, expanded[3]-expanded[1]):
                            continue
                        expanded = candidate
                        included.add(('named', j))
                        named_absorbed.add(tuple(round(v, 1) for v in block))
                        changed = True
            math_zones[i] = [expanded[0]-.5, expanded[1]-.5, expanded[2]+.5, expanded[3]+.5]
        protected = [{"id": r["id"], "rect": r["bbox"], "type": "figure"}
                     for r in source if r["type"] == "figure"]
        # Native tables and vector figures receive protection even without image
        # objects. Their labels are recorded rather than silently filtered out.
        for kind, objects in (("table", tables), ("figure", vectors)):
            for box in objects:
                protected.append({"id": stable_id(pn, box, "", kind[0].upper()), "rect": box, "type": kind})
        entries = []
        for r in source:
            if r["type"] == "figure":
                continue
            if r.get("stacked_selectivity"):
                entries.append(_stacked_formula_entry(r, pn))
                continue
            split = _line_groups(r, tables, prose, images+vectors+decorative_ink, repeated, bounds)
            for kind, box, text, lines in split:
                if not text:
                    continue
                inline_parents = [parent for parent, p in prose_lines if p != box
                                  and (parent != r['id'] or len(split) > 1)
                                  and source_columns.get(parent) == source_columns.get(r["id"])
                                  and abs((box[1]+box[3])-(p[1]+p[3])) <= max(body, dominant(lines))*.95
                                  and max(p[0]-box[2], box[0]-p[2], 0) <= max(body, dominant(lines))*5]
                if len(text) <= 80:
                    inline_parents.extend(other["id"] for other in source
                                          if (other['id'] != r['id'] or len(split) > 1)
                                          and other.get("tight_bbox", other["bbox"]) in long_prose
                                          and other.get("tight_bbox", other["bbox"]) != box
                                          and (contains(other.get("tight_bbox", other["bbox"]), box)
                                               or (max(other["bbox"][1]-box[3], box[1]-other["bbox"][3], 0) <= body*1.5
                                                   and min(other["bbox"][2], box[2]) > max(other["bbox"][0], box[0]))))
                displayed_math = (not re.search(r"\b[a-z]{4,}\b", text)
                                  and not re.search(r'\b(?:eq(?:uation|n)?)\.?\s*\d+\s*:?\s*$', text, re.I)
                                  and (bool(MATH.search(text)) or len(text) <= 80 and any(contains(z, box) for z in math_zones)))
                centers = [(visible_line_bbox(l, box)[1]+visible_line_bbox(l, box)[3])/2 for l in lines]
                multiline_numbers = (not re.search(r"[a-z]{3,}", text) and len(centers) > 1
                                     and max(centers)-min(centers) > max(body, dominant(lines))*1.5)
                # A named equation operand absorbed onto a math baseline is part
                # of the displayed equation; an inflated prose bbox merely
                # grazing it vertically must not pull it back to inline prose.
                if displayed_math or multiline_numbers or any(
                        intersects(tuple(round(v, 1) for v in b), tuple(round(v, 1) for v in box))
                        for b in named_absorbed):
                    inline_parents = []
                ident = r["id"] if len(split) == 1 else stable_id(pn, box, text)
                spans = [s for l in lines for s in l.get("spans", []) if s.get("text", "").strip()]
                size = dominant(lines)
                entry = {"id": ident, "source_page": pn, "rect": list(box), "source_text": text,
                         "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                         "type": kind, "translation_required": True, "translation": "",
                         "size": size, "source_font_pt": size, "leading": 1.17, "min_scale": .88,
                         "original_ids": [r["id"]]}
                if kind in {"reference-heading", "post-reference-heading"}:
                    entry["role"] = kind
                    kind = "paragraph"
                entry["original_bbox"] = list(r["bbox"])
                entry["source_lines"] = [{"text": block_text({"lines": [l]}),
                                           "bbox": visible_line_bbox(l, box),
                                           "original_bbox": list(l.get("bbox", box)),
                                           "spans": deepcopy(l.get("spans", []))} for l in lines]
                result["id_to_original"].setdefault(ident, {**originals[r["id"]], "parent_id": r["id"], "bbox": list(box), "text": text})
                if spans and all(s.get("flags", 0)&16 or "bold" in s.get("font", "").lower() for s in spans):
                    entry["bold"] = True
                h = bounds[3]-bounds[1]
                key = re.sub(r"\d+", "#", text).strip().lower()
                top_header = box[3] < bounds[1]+h*.105
                bot_footer = box[1] > bounds[3]-h*.08
                boundary = top_header or bot_footer
                page_num_boundary = box[3] < bounds[1]+h*.08 or bot_footer
                if (boundary and key in repeated) or (page_num_boundary and re.fullmatch(r"\d+", text)):
                    kind = "header" if top_header else "footer"
                caption = CAPTION.match(text)
                if not caption and lines:
                    # A standalone numbered label on its own line is established
                    # caption formatting, even without terminal punctuation.
                    label = block_text({"lines": [lines[0]]})
                    caption = re.fullmatch(r"(?P<kind>Fig(?:ure)?\.?|Table)\s*(?P<number>\d+)[.:]?", label, re.I)
                if caption and kind == "paragraph":
                    objects = tables if caption["kind"].lower() == "table" else images+vectors
                    lead_bold = bool(spans and (spans[0].get("flags", 0)&16 or "bold" in spans[0].get("font", "").lower()))
                    # Caption grammar AND proximity AND typographic separation.
                    formatted = bool(caption) and (size <= max(body, page_body)*1.05 or entry.get("bold") or lead_bold)
                    if formatted and _near(box, objects, size):
                        kind = "caption"
                        target = ("table" if caption["kind"].lower() == "table" else "fig")+"/"+caption["number"]
                        result["targets"].setdefault(target, {"source_page": pn, "point": box[:2]})
                    elif caption:
                        issues.append({"page": pn, "id": ident, "check": "caption-candidate-without-geometry",
                                       "action": "Review caption candidate; it remains translatable prose."})
                # Table cells directly below a native Table caption: short,
                # sentence-free, not intersecting long prose. Preserve the cell
                # instead of erasing and re-typesetting it as paragraph text.
                # Multi-line cells (column headers with values underneath) may
                # exceed a single short line; several short lines are still
                # unmistakably cell-shaped.
                cell_shaped = len(text) <= 40 or (len(text) <= 80 and len(text.split("\n")) >= 3)
                # Inflated bboxes of a paragraph BELOW the table can mark the
                # last cell row as an inline fragment; caption proximity plus a
                # sentence-free short block outweighs that geometry noise.
                if (kind == "paragraph" and cell_shaped
                        and not re.search(r"[.!?]\s*$", text.strip())
                        and not any(intersects(box, p) for p in long_prose)):
                    for cap in table_caption_zones:
                        below = 0 <= box[1]-cap[3] <= 250
                        aligned = min(box[2], cap[2])-max(box[0], cap[0]) >= .5*min(box[2]-box[0], cap[2]-cap[0])
                        if below and aligned:
                            kind = "table"
                            reason = "native-table"
                            break
                narrative_mention = bool(re.match(r"^(?:Fig(?:ure)?\.?|Table)\s*\d+\s+[A-Za-z]", text, re.I))
                prose_sentence = len(re.findall(r"[A-Za-z]+", text)) >= 6 and bool(re.search(r"[.!?]\s*$", text))
                # A continuation line of running prose (`...respectively, in
                # solutions at pH < 3`) is not a plot label, even close to a
                # figure: several ordinary lowercase words plus clause
                # punctuation are prose evidence no axis label produces.
                prose_like = len(re.findall(r"[a-z]{4,}", text)) >= 2 and bool(re.search(r"[,;]", text))
                if (kind in {"paragraph", "formula"} and not caption and not prose_like and any(contains(z, box) for z in label_zones)
                        and (len(text) <= 100 or not re.search(r"[a-z]{3,}", text))
                        and not narrative_mention and not prose_sentence and not inline_parents):
                    kind = "figure"
                elif (kind == "paragraph" and len(text) <= 80 and not inline_parents
                      and (not re.search(r"\b[a-z]{4,}\b", text)
                           or any(intersects(tuple(round(v, 1) for v in b), tuple(round(v, 1) for v in box)) for b in named_absorbed))
                      and not re.search(r'\b(?:then|where|thus|hence|with|from|given|using|is|are|was|were|be|the)\b',text,re.I)
                      and not (text.upper()==text and len(re.findall(r'[A-Z]',text))>=6)
                      and any(contains(z, box) for z in math_zones)):
                    kind = "formula"
                # A bare equation-number fragment (`2).`) is native equation
                # furniture. Proximity to a figure's label zone can misfile it
                # as a plot label; the math zone is the stronger evidence.
                if (re.fullmatch(r"\(?\d{1,3}\)[.,;:]?", text.strip())
                        and not inline_parents and any(contains(z, box) for z in math_zones)):
                    kind = "formula"; reason = "native-formula"
                if pn == pages[0]["source_page"] and kind == "paragraph" and size >= body*1.25 and box[1] < min(metadata_limit,bounds[1]+h*.4):
                    kind = "title"
                # Large prose above the abstract is stronger title evidence
                # than the expanded label zone of a nearby publisher graphic.
                if (pn==pages[0]['source_page'] and box[3]<metadata_limit and size>=body*1.4
                        and len(re.findall(r'[A-Za-z]{3,}',text))>=4
                        and not CAPTION.match(text) and not any(contains(z,box) for z in equation_zones)):
                    kind='title';inline_parents=[]
                if kind == "paragraph" and re.match(r"^abstract\b", text, re.I):
                    kind = "abstract"
                if normalize_heading(text).replace(" ", "") == "articleinfo":
                    entry["role"] = "metadata"
                    kind = "header"
                rotated = any(abs(l.get("dir", [1, 0])[0]-1) > .001 or abs(l.get("dir", [1, 0])[1]) > .001 for l in lines)
                # A numbered-equation zone encloses the equation AND its variable
                # definitions (`where Jw is the water flux...`). Those definition
                # lines are prose: keep them translatable unless the block is
                # equation-shaped itself (math operator, bare number label) or
                # carries no prose words at all.
                if any(contains(z,box) for z in equation_zones):
                    if _formula(text) or not re.search(r"[A-Za-z]{4,}", text):
                        kind='formula';inline_parents=[]
                if (pn==pages[0]['source_page'] and 'ACCESS' in text
                        and 'Metrics & More' in text and 'Article Recommendations' in text):
                    kind='header';inline_parents=[];entry['role']='publisher-navigation'
                reason = None
                if kind in {"formula", "table", "figure", "header", "footer"}:
                    reason = "native-"+kind
                overlaps_prose = [p for p in prose if p != r.get("tight_bbox", r["bbox"]) and intersects(box, p)]
                # A threshold value (`≥99.0%`) is a unit expression; keep it
                # native so the original glyphs survive.
                if UNIT.fullmatch(re.sub(r"^[≥≤><]\s*", "", text)) and not overlaps_prose:
                    reason = "native-unit"
                if overlaps_prose and len(text) < 25 and kind == "paragraph":
                    entry["role"] = "inline-fragment"
                    entry["paragraph_start"] = False
                    entry["inline_fragment_of"] = [other["id"] for other in source if other.get("tight_bbox", other["bbox"]) in overlaps_prose]
                    issues.append({"page": pn, "id": ident, "check": "inline-fragment-placement",
                                   "action": "Review fragment position against parent prose; preserve scientific notation in translation."})
                if inline_parents and kind in {"paragraph", "formula"}:
                    kind = "paragraph"
                    reason = None
                    entry.update(role="inline-fragment", paragraph_start=False, inline_fragment_of=sorted(set(inline_parents)))
                if rotated:
                    reason = "rotated-text"
                    if kind == 'title':kind = 'header'
                if text.strip() in {'■','●','•','▪','◼','◆'}:
                    kind='figure';reason='native-decorative-marker'
                # Author and publication metadata remains original, with its role
                # exposed independently from the required public block taxonomy.
                if pn == pages[0]["source_page"] and kind == "paragraph" and box[1] < metadata_limit:
                    titles = [e for e in entries if e["type"] == "title"]
                    title_above=[t for t in entries if t['type']=='title' and t['rect'][3]<=box[1]]
                    author_line = bool(title_above and box[1]-title_above[-1]["rect"][3] < body*5
                                       and len(text) < 200 and (',' in text or ' and ' in text)
                                       and not re.search(r"[!?;]", text))
                    if (META.search(text) and len(text) <= 350) or author_line:
                        reason = "author-publication-metadata"
                        entry["role"] = "metadata"
                if (pn == pages[0]["source_page"] and len(text) <= 450
                        and re.search(r"(?:corresponding author|e-?mail address|available online|all rights reserved|©|^https?://(?:dx\.)?doi\.org/|^Received\s+\d)", text, re.I)):
                    reason = "author-publication-metadata"
                    entry["role"] = "metadata"
                    kind = "paragraph"
                if (pn == pages[0]['source_page'] and box[1] < metadata_limit and len(text) <= 450
                        and re.search(r'(?:^\s*DOI\s*:|\bpublished by\b|\bset by the publisher\b)',text,re.I)):
                    kind='header';reason='author-publication-metadata';entry['role']='metadata'
                # Running headers must not be reclassified as graph labels by a
                # nearby full-page figure. Preserve their boundary provenance.
                if boundary and key in repeated:
                    kind='header' if top_header else 'footer'
                    reason='native-'+kind
                if kind in {"paragraph", "abstract", "caption"} and box[3] - box[1] >= body * 2:
                    for img in images:
                        if (min(box[2], img[2]) > max(box[0], img[0])
                                and box[1] < img[1] - body and 0 < box[3] - img[1] <= 2.5
                                and all(s.get("origin", [0, box[1]])[1] <= img[1]
                                        for l in lines for s in l.get("spans", []) if s.get("text", "").strip())):
                            box = [box[0], box[1], box[2], round(img[1] - 0.05, 3)]
                            entry["rect"] = list(box)
                entry["type"] = kind
                if reason:
                    entry.update(translation_required=False, preservation_reason=reason)
                entries.append(entry)
        # Bidirectional sweep: forward absorption turns prose-like equation
        # operands native (named_absorbed); reverse the check for formula blocks
        # that still carry prose words without any math operator — a prose line
        # wrap misread as a displayed equation. Surface it at prepare time so it
        # is resolved with the other review items, not found at visual QA.
        for entry in entries:
            if entry.get("preservation_reason") == "native-stacked-selectivity":
                continue
            if (entry["type"] == "formula"
                    and re.search(r"\b[A-Za-z]{3,}\b", entry["source_text"])
                    and not MATH.search(entry["source_text"])
                    and not any(intersects(tuple(round(v, 1) for v in b),
                                           tuple(round(v, 1) for v in entry["rect"]))
                                for b in named_absorbed)):
                issues.append({"page": pn, "id": entry["id"], "check": "formula-with-prose-candidate",
                               "action": "Review formula classification: block has prose words without math operators; may be a prose line wrap to translate."})
        # Caption text extraction sometimes splits a short tail (`2).`) into a
        # separate object whose rectangle lies inside the caption's. It is an
        # inline fragment of that caption, not a plot element and not an
        # independent column of prose. Keep it translatable and tied to its
        # parent so reading order stays correct.
        captions_here = [e for e in entries if e["type"] == "caption"]
        for cap in captions_here:
            c = list(cap["rect"])
            for other in entries:
                if other is cap:
                    continue
                o = other["rect"]
                if len(other["source_text"]) <= 25 and o[0] >= c[0]-1 and o[1] >= c[1]-1 and o[2] <= c[2]+1 and o[3] <= c[3]+1:
                    other.update(type="paragraph", role="inline-fragment",
                                 paragraph_start=False, inline_fragment_of=[cap["id"]],
                                 translation_required=True)
                    other.pop("preservation_reason", None)
                elif (other["type"] == "paragraph"
                      and abs(float(other.get("source_font_pt") or 0) - float(cap.get("source_font_pt") or 0)) <= 0.5
                      and abs(o[0] - c[0]) <= 4 and -12 <= o[1] - c[3] <= 6):
                    other["type"] = "caption"
                    c[3] = max(c[3], o[3])
        entries = reading_order(entries, bounds)
        titles = [b for b in entries if b["type"] == "title"]
        if titles:
            for candidate in titles:
                above=[t for t in titles if t is not candidate and t['rect'][3]<=candidate['rect'][1]
                       and candidate['rect'][1]-t['rect'][3]<body*5 and t.get('size',0)>candidate.get('size',0)]
                if above and (',' in candidate['source_text'] or ' and ' in candidate['source_text']) and len(candidate['source_text'])<200:
                    candidate.update(type='paragraph',role='metadata',translation_required=False,preservation_reason='author-publication-metadata')
            titles=[b for b in entries if b['type']=='title']
            seed=max(titles,key=lambda b:len(b['source_text']))
            for candidate in titles:
                if (candidate is not seed and candidate['rect'][1]>=seed['rect'][3]
                        and candidate['rect'][1]-seed['rect'][3]<body*5
                        and (',' in candidate['source_text'] or ' and ' in candidate['source_text'])
                        and len(candidate['source_text'])<200):
                    candidate.update(type='paragraph',role='metadata',translation_required=False,preservation_reason='author-publication-metadata')
            titles=[b for b in entries if b['type']=='title']
        if len(titles) > 1:
            seed = max(titles, key=lambda b: len(b["source_text"]))
            for candidate in titles:
                gap = max(seed["rect"][1]-candidate["rect"][3], candidate["rect"][1]-seed["rect"][3], 0)
                if candidate is not seed and gap > body*2:
                    candidate.update(type="header", role="publication-masthead", translation_required=False,
                                     preservation_reason="publication-metadata")
        for entry in entries:
            text = entry["source_text"]
            if REF_HEAD.fullmatch(normalize_heading(text)):
                in_refs = True
                seen_ref_head = True
                reference_style = None
                entry["role"] = "reference-heading"
                continue
            # Close the bibliography before considering numbering/year patterns:
            # declarations frequently contain dates or numbered clauses too.
            if in_refs and POST_REF.match(normalize_heading(text)):
                in_refs = False
                last_ref = None
            elif (seen_ref_head and not in_refs and not POST_REF.match(normalize_heading(text))
                  and re.search(r"\b(?:19|20)\d{2}[a-z]?\b", text)
                  and (re.search(r"\bdoi\b|https?://", text, re.I)
                       or (len(re.findall(r"[A-Z]\.", text)) >= 2 and re.search(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)", text)))):
                in_refs = True
            # Floating figures/captions may interrupt a bibliography and then
            # numbering resumes. Do not append a caption to the preceding ref.
            caption_candidate=bool(CAPTION.match(text))
            if in_refs and entry["type"] not in {"header", "footer", "figure", "caption"} and not caption_candidate and entry.get("preservation_reason") != "rotated-text":
                starts = list(re.finditer(r"(?m)^[ \t]*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})\.)(?:[ \t]+|$)", text))
                accepted = []
                for match in starts:
                    style = next(i for i, v in enumerate(match.groups()) if v is not None)
                    n = int(next(v for v in match.groups() if v is not None))
                    remainder = text[match.end():].split("\n", 1)[0].strip()
                    bare = not remainder
                    if reference_style is not None and style != reference_style:
                        continue
                    if bare and (n in refs_by_number or last_ref is not None and n != last_ref+1 or last_ref is None and n != 1):
                        continue
                    accepted.append(match)
                    if reference_style is None:
                        reference_style = style
                starts = accepted
                if starts:
                    prefix = text[:starts[0].start()].strip()
                    if prefix and last_ref in refs_by_number:
                        refs_by_number[last_ref]["text"] += "\n"+prefix
                    entry.update(type="reference", translation_required=False, preservation_reason="bibliography")
                    for i, match in enumerate(starts):
                        n = int(next(v for v in match.groups() if v is not None))
                        value = text[match.end():starts[i+1].start() if i+1 < len(starts) else None].strip()
                        line_index = text[:match.start()].count("\n")
                        source_lines = entry["source_lines"]
                        destination = source_lines[line_index]["bbox"] if line_index < len(source_lines) else entry["rect"]
                        ref = {"number": n, "text": value, "source_page": pn, "rect": destination,
                               "source_block_id": entry["id"], "source_block_ids": [entry["id"]]}
                        if n in refs_by_number:
                            issues.append({"check": "duplicate-reference-number", "number": n, "page": pn})
                        else:
                            refs_by_number[n] = ref
                            result["targets"][f"ref/{n}"] = {"source_page": pn, "point": destination[:2]}
                            last_ref = n
                        result["reference_candidates"].append(ref)
                elif last_ref is None and re.search(r"\b(?:19|20)\d{2}\b", text) and (re.search(r"\bdoi\b|https?://", text, re.I) or len(re.findall(r"[A-Z]\.", text)) >= 2):
                    entry.update(type="reference", translation_required=False, preservation_reason="bibliography")
                    issues.append({"page": pn, "id": entry["id"], "check": "unnumbered-reference-candidate",
                                   "action": "Review author-year bibliography; preserve original metadata after review."})
                else:
                    entry.update(type="reference", translation_required=False, preservation_reason="bibliography-continuation")
                    if last_ref in refs_by_number:
                        ref = refs_by_number[last_ref]
                        ref["text"] += "\n"+text
                        ref.setdefault("source_block_ids", []).append(entry["id"])
                        entry["reference_continuation_of"] = last_ref
                    issues.append({"page": pn, "id": entry["id"], "check": "bibliography-continuation-candidate",
                                   "action": "Original unnumbered bibliography text is preserved; review continuation versus an unknown post-bibliography section."})
            if entry["type"] == "formula":
                numbers = re.findall(r"\((\d{1,3})\)\s*$", text)
                if not numbers:
                    numbers = re.findall(r"^\s*\((\d{1,3})\)\s+", text)
                for n in numbers:
                    result["targets"].setdefault(f"eq/{n}", {"source_page": pn, "point": entry["rect"][:2]})
        # Tiny equation numbers, single chemical formulas and reference tails
        # are native content. Translating them adds no meaning and overflows
        # their micro rectangles with full-width punctuation. Caption tails are
        # exempt: `2).` belongs to the translated caption sentence.
        for entry in entries:
            if (entry.get("translation_required", True)
                    and entry["type"] in {"paragraph", "formula"}
                    and not entry.get("inline_fragment_of")
                    and native_micro_fragment(entry["source_text"], entry["rect"])):
                entry.update(type="formula", translation_required=False,
                             preservation_reason="native-micro-equation-or-formula")
        for entry in entries:
            # Compact 8 pt journals must not be forced to 9.5 pt with min_scale 1.
            # Bold headings and titles keep the size chosen above.
            if (entry.get("translation_required", True)
                    and entry.get("type") in {"paragraph", "abstract"}
                    and not entry.get("bold")):
                source_pt = entry.get("source_font_pt", entry.get("size"))
                entry["size"] = adapted_body_size(source_pt)
                entry["min_scale"] = 0.85
            if not entry.get("translation_required", True):
                continue
            marked = script_markup(entry.get("source_lines") or [])
            if not marked:
                continue
            visible = re.sub(r"\s+", "", re.sub(r"</?(?:sup|sub)>", "", marked, flags=re.I))
            current = re.sub(r"\s+", "", entry.get("source_text", ""))
            if visible == current:
                entry["source_text"] = marked
                entry["source_text_sha256"] = hashlib.sha256(marked.encode()).hexdigest()
        result["blocks"].extend(entries)
        # Thin rules and decorative drawing objects can obstruct blank-space
        # allocation without being scientific figures eligible for protection.
        result["pages"].append({"source_page": pn, "rect": bounds, "protected": protected,
                                "spacing_obstacles": deepcopy(page.get("drawing_clusters", []))})
    result["references"] = sorted(refs_by_number.values(), key=lambda r: r["number"])
    known = result["targets"]
    for b in result["blocks"]:
        links = []
        for match in re.finditer(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]", b["source_text"]):
            nums = []
            for part in match[1].split(","):
                ends = re.split(r"[–-]", part.strip())
                nums.extend(range(int(ends[0]), int(ends[-1])+1) if len(ends) == 2 and int(ends[-1])-int(ends[0]) <= 100 else [int(ends[0])])
            links.extend({"kind": "ref", "number": n, "target": f"ref/{n}", "source_span": list(match.span())}
                         for n in nums if f"ref/{n}" in known)
        for match in re.finditer(r"\b(Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?)\s*\(?([0-9]+)\)?", b["source_text"], re.I):
            kind = "fig" if match[1].lower().startswith("fig") else "table" if match[1].lower() == "table" else "eq"
            target = kind+"/"+match[2]
            if target in known and not re.search(r"supplement|supporting", b["source_text"][max(0, match.start()-25):match.start()], re.I):
                links.append({"kind": kind, "number": int(match[2]), "target": target, "source_span": list(match.span())})
        b["inline_links"] = links
    result["expected_block_ids"] = [b["id"] for b in result["blocks"]]
    result["prepare_summary"] = {"blocks": len(result["blocks"]), "translation_required": sum(b["translation_required"] for b in result["blocks"]),
                                 "body_font_pt": body, "types": dict(Counter(b["type"] for b in result["blocks"])),
                                 "drawing_clusters": "available" if all("drawing_clusters" in p for p in pages) else "missing"}
    result["need_review"] = bool(issues)
    return result
