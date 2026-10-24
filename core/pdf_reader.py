"""Local PDF inspection. This module does not translate or mutate PDFs."""
from __future__ import annotations

import hashlib
import json
import io
from pathlib import Path

import pymupdf as fitz


def stable_id(page, bbox, text, prefix="T"):
    """Content/location identity, independent of extraction order and other blocks.

    Coordinates are rounded to 0.01pt to absorb insignificant float serialization
    noise. Identical co-located objects intentionally share an identity and are
    reported as duplicates, rather than assigned order-dependent suffixes.
    """
    payload = [int(page), [round(float(v), 2) for v in bbox], text]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()
    return f"P{int(page):03}{prefix}{digest[:20]}"


def block_text(block):
    return "\n".join("".join(s.get("text", "") for s in line.get("spans", []))
                     for line in block.get("lines", [])).strip()


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def visible_line_bbox(line, fallback=None):
    boxes = [s.get("nonwhitespace_bbox") or s.get("bbox") for s in line.get("spans", []) if s.get("text", "").strip()]
    boxes = [b for b in boxes if b is not None]
    return _union(boxes) if boxes else list(line.get("bbox", fallback))


def annotate_visible_geometry(blocks, raw_blocks):
    """Keep legacy bounds; add non-whitespace glyph bounds from matching raw spans.

    This removes oversized *blank* font spans only. It never guesses a glyph's
    ink outline from font size or shrinks mathematical nonblank glyph bounds.
    """
    for block, raw in zip(blocks, raw_blocks):
        if block.get("type") != 0 or raw.get("type") != 0:
            continue
        for line, raw_line in zip(block.get("lines", []), raw.get("lines", [])):
            visible = []
            for span, raw_span in zip(line.get("spans", []), raw_line.get("spans", [])):
                if "".join(c.get("c", "") for c in raw_span.get("chars", [])) != span.get("text", ""):
                    # A mismatched raw extraction cannot justify reducing bounds.
                    if span.get("text", "").strip():
                        visible.append(span["bbox"])
                    continue
                chars = [c["bbox"] for c in raw_span.get("chars", []) if c.get("c", "").strip()]
                span["nonwhitespace_bbox"] = _union(chars) if chars else None
                visible.extend(chars)
            if visible:
                line["nonwhitespace_bbox"] = _union(visible)
        visible_lines = [l["nonwhitespace_bbox"] for l in block.get("lines", []) if l.get("nonwhitespace_bbox")]
        if visible_lines:
            block["nonwhitespace_bbox"] = _union(visible_lines)


def _decorative_candidate(line, span):
    """Only a leading standalone marker next to a textual heading, never math."""
    if span.get('text','').strip() not in {'■','▪','●','•','◼'}:
        return False
    if line.get('dir',(1,0)) != (1,0) or line.get('wmode',0):
        return False
    others=[s for s in line.get('spans',[]) if s is not span and s.get('text','').strip()]
    if not others or any(s['bbox'][0]<span['bbox'][0] for s in others):return False
    first=others[0]
    text=''.join(s.get('text','') for s in others).strip()
    if any(c in text for c in '=+−∑∫√×÷≤≥<>[]{}') or any('\u0370'<=c<='\u03ff' for c in text):
        return False
    letters=[c for c in text if c.isalpha()]
    heading=bool(first.get('flags',0)&16) or (len(letters)>=3 and text.upper()==text)
    return (heading and len(text)<=120 and len(letters)>=3
            and 0<=first['bbox'][0]-span['bbox'][2]<=2*first.get('size',10))


def _cff_outline_bounds(font_buffer, glyph_id):
    """Exact embedded CFF CharString bounds, not the subset's global FontBBox."""
    try:
        from fontTools.cffLib import CFFFontSet
        from fontTools.pens.boundsPen import BoundsPen
    except ImportError:
        return None
    cff=CFFFontSet();cff.decompile(io.BytesIO(font_buffer),None)
    if len(cff.fontNames)!=1:return None
    top=cff[cff.fontNames[0]]
    if not 0<=glyph_id<len(top.charset):return None
    glyph_name=top.charset[glyph_id]
    if glyph_name=='.notdef':return None
    charstring=top.CharStrings[glyph_name]
    pen=BoundsPen(None);charstring.draw(pen)
    if pen.bounds is None:return None
    matrix=list(top.FontMatrix)
    # Rotation/skew/CID transforms require additional PDF text matrices; leave
    # the original protective bounds whenever those cannot be proven here.
    if matrix[1] or matrix[2] or matrix[0]<=0 or matrix[3]<=0:return None
    return pen.bounds,matrix,float(charstring.width),glyph_name


def annotate_decorative_ink(page, blocks):
    """Add proven native marker outlines; retain all original span/block boxes."""
    candidates=[(b,l,s) for b in blocks if b.get('type')==0
                for l in b.get('lines',[]) for s in l.get('spans',[])
                if _decorative_candidate(l,s)]
    if not candidates:return
    traces=page.get_texttrace();fonts=page.get_fonts(full=True);outline_cache={}
    for block,line,span in candidates:
        matches=[t for t in traces if t.get('font')==span.get('font')
                 and t.get('dir')==(1,0) and t.get('wmode',0)==0 and t.get('type')==0
                 and abs(t.get('size',0)-span.get('size',0))<.02
                 for char in t.get('chars',[]) if char[0]==ord(span['text'].strip())
                 and max(abs(char[2][i]-span['origin'][i]) for i in (0,1))<.02]
        if len(matches)!=1:continue
        trace=matches[0]
        chars=[c for c in trace['chars'] if c[0]==ord(span['text'].strip())
               and max(abs(c[2][i]-span['origin'][i]) for i in (0,1))<.02]
        if len(chars)!=1:continue
        char=chars[0]
        def native_name(name):
            head,sep,tail=name.partition('+')
            return tail if sep and len(head)==6 and head.isupper() else name
        embedded=[f for f in fonts if native_name(f[3])==span['font'] and f[1]=='cff']
        if len(embedded)!=1:continue
        xref=embedded[0][0];key=(xref,char[1])
        try:
            if key not in outline_cache:
                outline_cache[key]=_cff_outline_bounds(page.parent.extract_font(xref)[3],char[1])
            outline=outline_cache[key]
        except Exception:
            # Unsupported composite/CID CharStrings or malformed font tables
            # cannot justify shrinking native protection. Keep legacy bounds.
            continue
        if outline is None:continue
        bounds,matrix,width,glyph_name=outline
        size=float(span['size']);ox,oy=char[2]
        expected_advance=width*matrix[0]*size
        actual_advance=char[3][2]-char[3][0]
        # A modified PDF width or horizontal scale cannot be silently ignored.
        if expected_advance<=0 or abs(expected_advance-actual_advance)>.02:continue
        x0,y0,x1,y1=bounds
        box=[ox+(x0*matrix[0]+matrix[4])*size,
             oy-(y1*matrix[3]+matrix[5])*size,
             ox+(x1*matrix[0]+matrix[4])*size,
             oy-(y0*matrix[3]+matrix[5])*size]
        r=fitz.Rect(box)
        if r.is_empty or not fitz.Rect(span['bbox']).contains(r):continue
        span['nonwhitespace_bbox']=box
        span['native_ink_proof']={'method':'embedded-CFF-CharString-outline',
            'font_xref':xref,'glyph_id':char[1],'glyph_name':glyph_name,
            'font_matrix':matrix,'outline_bounds':list(bounds),'origin':list(char[2]),
            'size':size,'original_bbox':list(span['bbox']),
            'role_evidence':'leading-marker-next-to-textual-heading'}
        line['nonwhitespace_bbox']=visible_line_bbox(line)
        visible_lines=[visible_line_bbox(l) for l in block.get('lines',[]) if block_text({'lines':[l]})]
        if visible_lines:block['nonwhitespace_bbox']=_union(visible_lines)


def records_from_pages(pages):
    records, originals, issues = [], {}, []
    for page in pages:
        pn = page["source_page"]
        for index, block in enumerate(page.get("blocks", [])):
            is_image = block.get("type") == 1
            text = "" if is_image else block_text(block)
            if not text and not is_image:
                continue
            # An image hash is available on freshly parsed PDFs. Geometry remains
            # sufficient for legacy inspections without embedded image bytes.
            key = block.get("image_sha256", "") if is_image else text
            ident = stable_id(pn, block["bbox"], key, "I" if is_image else "T")
            record = {"id": ident, "page": pn, "type": "figure" if is_image else "text",
                      "bbox": list(block["bbox"]), "text": text,
                      "tight_bbox": list(block.get("nonwhitespace_bbox", block["bbox"])),
                      "lines": block.get("lines", []), "original_block_index": index}
            visible_lines = [visible_line_bbox(l, block["bbox"]) for l in block.get("lines", []) if block_text({"lines": [l]})]
            if visible_lines:
                record["tight_bbox"] = _union(visible_lines)
            records.append(record)
            origin = {"page": pn, "block_index": index, "bbox": list(block["bbox"]),
                      "text": text, "original_type": block.get("type", 0)}
            if ident in originals:
                issues.append({"check": "duplicate-object-identity", "id": ident,
                               "page": pn, "action": "Review duplicate co-located extraction objects."})
                originals[ident].setdefault("duplicates", []).append(origin)
            else:
                originals[ident] = origin
    return records, originals, issues


def parse(source, render_dir=None):
    """Return legacy inspection plus unified records and ID-to-original mapping.

    Empty/scanned pages and table detector failures are explicit review issues.
    Optional renders are 120dpi inspection aids, never substituted for source.
    """
    source = Path(source)
    output = Path(render_dir) if render_dir is not None else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    pages, issues = [], []
    with fitz.open(source) as doc:
        for pn, page in enumerate(doc, 1):
            blocks = page.get_text("dict", sort=False)["blocks"]
            annotate_visible_geometry(blocks, page.get_text("rawdict", sort=False)["blocks"])
            annotate_decorative_ink(page,blocks)
            for block in blocks:
                if "image" in block:
                    block["image_sha256"] = hashlib.sha256(block["image"]).hexdigest()
                block.pop("image", None)
                block.pop("mask", None)
            clusters = [list(r) for r in page.cluster_drawings()] if hasattr(page, "cluster_drawings") else []
            rules = [list(path["rect"]) for path in page.get_drawings()
                     if path["rect"].width >= 50 and path["rect"].height <= 2]
            tables = []
            if hasattr(page, "find_tables"):
                try:
                    tables = [{"bbox": list(t.bbox), "cells": [list(c) if c else None for c in t.cells],
                               "rows": t.row_count, "columns": t.col_count}
                              for t in page.find_tables().tables]
                except Exception as exc:
                    issues.append({"page": pn, "check": "table-detection-failed",
                                   "detail": str(exc), "action": "Review table geometry manually."})
            pages.append({"source_page": pn, "rect": list(page.rect), "text": page.get_text(),
                          "blocks": blocks, "drawing_clusters": clusters, "drawing_rules": rules, "tables": tables})
            if not any(block_text(b) for b in blocks if b.get("type") != 1):
                issues.append({"page": pn, "check": "no-extractable-text",
                               "action": "Review blank/scan page; local text extraction cannot certify coverage."})
            if output:
                page.get_pixmap(dpi=120).save(output / f"page-{pn:03}.png")
        count = len(doc)
    records, originals, identity_issues = records_from_pages(pages)
    issues.extend(identity_issues)
    return {"schema_version": "4.0", "source": str(source.resolve()),
            "page_count": count, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "pages": pages, "records": records, "id_to_original": originals,
            "issues": issues, "need_review": bool(issues)}


def _script_position(span, spans):
    """Return 'sup' or 'sub' for a short raised/lowered span, else None.

    PDF superscript uses flag bit 0. Subscripts and many citation digits are
    only visible as a smaller size on a shifted baseline. A whole line of
    small type (no larger neighbor) is body text, not a script.
    """
    text = span.get('text', '')
    if not text or text.strip() == '' or len(text.strip()) > 16:
        return None
    if '<sup>' in text or '<sub>' in text:
        return None
    flags = int(span.get('flags') or 0)
    if flags & 1:
        return 'sup'
    try:
        size = float(span.get('size') or 0)
    except (TypeError, ValueError):
        return None
    origin = span.get('origin')
    if not origin or size <= 0:
        return None
    bases = []
    for other in spans:
        if other is span or not other.get('text', '').strip():
            continue
        try:
            other_size = float(other.get('size') or 0)
        except (TypeError, ValueError):
            continue
        if other_size > size / .85 and other.get('origin'):
            bases.append((other_size, other))
    if not bases:
        return None
    base_size, base = max(bases, key=lambda item: item[0])
    if not 0 < size < base_size * .85:
        return None
    try:
        delta = float(origin[1]) - float(base['origin'][1])
    except (TypeError, ValueError, IndexError):
        return None
    if abs(delta) < max(.4, base_size * .08):
        return None
    return 'sup' if delta < 0 else 'sub'


def script_markup(lines):
    """Serialize superscripts and subscripts already present in extracted spans.

    Visible characters stay identical to ``block_text``; only ``<sup>`` and
    ``<sub>`` are inserted. Existing tags are not wrapped again.
    """
    if not lines:
        return ''
    rendered = []
    for line in lines:
        spans = line.get('spans') or []
        parts = []
        for span in spans:
            text = span.get('text', '')
            if not text:
                continue
            position = _script_position(span, spans)
            if position:
                parts.append(f'<{position}>{text}</{position}>')
            else:
                parts.append(text)
        rendered.append(''.join(parts))
    return '\n'.join(rendered).strip()
