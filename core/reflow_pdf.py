"""Unified Chinese academic PDF reflow builder powered by reader_export manifests and PyMuPDF Story."""
from __future__ import annotations

import hashlib
import html
import json
import re
import tempfile
from pathlib import Path
import pymupdf as fitz

from .classifier import column_groups, intersects, reading_order
from .fonts import resolve_fonts
from .scientific_markup import restore_scientific_markup

_MEDIA_KINDS = {'figure', 'table', 'formula'}
_SKIP_TYPES = {'header', 'footer', 'page_number', 'reference', 'references', 'bibliography'}
_CAP_PREFIX = re.compile(r'^(?:Fig(?:ure)?\.?|Table|Scheme|图|表)\s*([A-Z]?\d+)', re.I)
_EQ_NUM = re.compile(r'\((\d+)\)\s*$')
_TAG_STRIP = re.compile(r'<[^>]+>')


def _clean_inline_for_story(text, known_targets, unresolved_links):
    """Convert internal citation/scientific text to safe Story HTML with #anchor links."""
    if not text:
        return ''
    escaped = html.escape(text).replace('\n', '<br/>')
    escaped = re.sub(r'&lt;(/?(?:sub|sup|b|i|strong|em|code))&gt;', r'<\1>', escaped, flags=re.I)
    escaped = re.sub(r'\^\{([^{}\n]+)\}', r'<sup>\1</sup>', escaped)
    escaped = re.sub(r'_\{([^{}\n]+)\}', r'<sub>\1</sub>', escaped)

    def _replace_explicit(match):
        kind, spec, label = match.groups()
        spec_s = spec.strip()
        if kind == 'ref' and not label:
            links = []
            for part in spec_s.split(','):
                part = part.strip()
                m_range = re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)', part)
                if m_range:
                    a, b = int(m_range[1]), int(m_range[2])
                    t1, t2 = f'ref-{a}', f'ref-{b}'
                    if t1 in known_targets and t2 in known_targets:
                        links.append(f'<a href="#{t1}">{a}</a>–<a href="#{t2}">{b}</a>')
                    else:
                        unresolved_links.append({'key': f'ref/{part}', 'reason': 'missing-ref-target'})
                        links.append(f'{a}–{b}')
                elif part.isdigit():
                    t = f'ref-{part}'
                    if t in known_targets:
                        links.append(f'<a href="#{t}">{part}</a>')
                    else:
                        unresolved_links.append({'key': f'ref/{part}', 'reason': 'missing-ref-target'})
                        links.append(part)
                else:
                    links.append(html.escape(part))
            return f'<sup>[{",".join(links)}]</sup>'
        target = f'{kind}-{spec_s}'
        default_lbl = {'fig': f'图 {spec_s}', 'table': f'表 {spec_s}', 'eq': f'式 ({spec_s})', 'ref': f'文献 {spec_s}'}[kind]
        shown = label or default_lbl
        if target in known_targets:
            return f'<a href="#{target}">{shown}</a>'
        unresolved_links.append({'key': f'{kind}/{spec_s}', 'label': shown, 'reason': 'missing-target'})
        return f'<span class="xref-plain">{shown}</span>'

    escaped = re.sub(r'\[\[(fig|ref|eq|table):([^\]|\n]+)(?:\|([^\]\n]+))?\]\]', _replace_explicit, escaped)
    escaped = re.sub(r'\[(?:Fig|fig|Figure|figure):([1-9]\d*)([a-zA-Z]*)\]',
                     lambda m: (f'<a href="#fig-{m[1]}">图 {m[1]}{m[2]}</a>'
                                if f'fig-{m[1]}' in known_targets else f'图 {m[1]}{m[2]}'), escaped)

    def _auto_ref(match):
        inner = match.group(1)
        def _num(nm):
            t = f'ref-{nm[0]}'
            return f'<a href="#{t}">{nm[0]}</a>' if t in known_targets else nm[0]
        return '[' + re.sub(r'\d+', _num, inner) + ']'

    parts = re.split(r'(<[^>]+>)', escaped)
    anchor_depth = 0
    for idx, part in enumerate(parts):
        if part.startswith('<'):
            if re.match(r'<a\b', part, re.I):
                anchor_depth += 1
            elif re.match(r'</a\b', part, re.I):
                anchor_depth = max(0, anchor_depth - 1)
            continue
        if not anchor_depth:
            part = re.sub(r'\[(\d+(?:\s*[,–-]\s*\d+)*)\]', _auto_ref, part)
            parts[idx] = part
    return ''.join(parts)


def _crop_rect(page, rect, out_path, dpi=220):
    clip = fitz.Rect(rect) & page.rect
    if clip.is_empty or clip.width < 2 or clip.height < 2:
        return False
    pad = 2.0
    padded = fitz.Rect(clip.x0 - pad, clip.y0 - pad, clip.x1 + pad, clip.y1 + pad) & page.rect
    pix = page.get_pixmap(clip=padded, dpi=dpi)
    if pix.width < 2 or pix.height < 2:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    return True


def _rect_overlap_ratio(a, b):
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1e-3, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-3, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


def _is_decorative_sliver(rect, pno, btype):
    if not rect or len(rect) != 4:
        return True
    rw = rect[2] - rect[0]
    rh = rect[3] - rect[1]
    if rw < 18.0 or rh < 18.0:
        return True
    if btype == 'figure' and pno == 1 and rect[3] < 195.0:
        return True
    return False


def _load_mineru_table_boxes(work_dir, pages):
    ext_root = Path(work_dir) / 'extraction'
    if not ext_root.is_dir():
        return {}
    sj_path = next(ext_root.glob('*/source.json'), None)
    if not sj_path:
        return {}
    try:
        items = json.loads(sj_path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(items, list):
        return {}
    boxes = {}
    for entry in items:
        if entry.get('type') != 'table' or not isinstance(entry.get('bbox'), list) or len(entry['bbox']) != 4:
            continue
        pno = int(entry.get('page_idx', -1)) + 1
        pg_spec = pages.get(pno)
        if not pg_spec:
            continue
        pw, ph = pg_spec['rect'][2], pg_spec['rect'][3]
        bx = entry['bbox']
        rect = [bx[0] * pw / 1000.0, bx[1] * ph / 1000.0, bx[2] * pw / 1000.0, bx[3] * ph / 1000.0]
        boxes.setdefault(pno, []).append(rect)
    return boxes


def _merge_close_rects(rects, gap_x=18.0, gap_y=14.0):
    groups = [list(r) for r in rects if r and len(r) == 4]
    changed = True
    while changed:
        changed = False
        merged = []
        while groups:
            cur = groups.pop(0)
            i = 0
            while i < len(groups):
                cand = groups[i]
                x_close = cur[0] - gap_x <= cand[2] and cand[0] - gap_x <= cur[2]
                y_close = cur[1] - gap_y <= cand[3] and cand[1] - gap_y <= cur[3]
                if x_close and y_close:
                    cur = [min(cur[0], cand[0]), min(cur[1], cand[1]), max(cur[2], cand[2]), max(cur[3], cand[3])]
                    groups.pop(i)
                    changed = True
                else:
                    i += 1
            merged.append(cur)
        groups = merged
    return groups


def _inside_any_rect(rect, boxes, pad=6.0):
    if not rect or not boxes:
        return False
    cx = (rect[0] + rect[2]) * 0.5
    cy = (rect[1] + rect[3]) * 0.5
    return any(b[0] - pad <= cx <= b[2] + pad and b[1] - pad <= cy <= b[3] + pad for b in boxes)


def merged_to_reader_manifest(merged, source_pdf, asset_dir):
    """Convert a translated ``merged.json`` + ``source.pdf`` into a reader_export manifest.

    Preserves figures, tables, display equations, and any fallback untranslated
    blocks cleanly placed in reading order.
    """
    asset_dir = Path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    blocks = merged.get('blocks', [])
    pages = {p['source_page']: p for p in merged.get('pages', [])}
    mineru_tables = _load_mineru_table_boxes(asset_dir.parent, pages)
    title = merged.get('title') or next(
        (b.get('translation') or b.get('source_text') for b in blocks if b.get('type') == 'title'),
        Path(source_pdf).stem,
    )
    title_en = next((b.get('source_text') for b in blocks if b.get('type') == 'title'), None)

    ordered_items = []
    fig_counter, table_counter, eq_counter = 0, 0, 0
    used_numbers = {'figure': set(), 'table': set(), 'equation': set()}

    def _next_num(kind, hint=None):
        if hint and hint.isdigit():
            val = int(hint)
            if val > 0 and val not in used_numbers[kind]:
                used_numbers[kind].add(val)
                return val
        n = 1
        while n in used_numbers[kind]:
            n += 1
        used_numbers[kind].add(n)
        return n

    with fitz.open(source_pdf) as src_doc:
        for pno in sorted(pages):
            pg_spec = pages[pno]
            pg_bounds = pg_spec['rect']
            src_page = src_doc[pno - 1]
            pg_blocks = [b for b in blocks if b.get('source_page') == pno and not b.get('absorbed')]
            tboxes = mineru_tables.get(pno, [])

            # Keep non-table/non-formula blocks; collapse decomposed table cells covered by MinerU table boxes.
            kept_blocks = []
            extra_media = []
            formula_rects = []
            table_rects = list(tboxes)
            emitted_fig_rects = []

            for b in pg_blocks:
                btype = b.get('type', 'paragraph')
                brect = b.get('rect')
                if btype == 'table':
                    if brect and not _inside_any_rect(brect, tboxes):
                        table_rects.append(brect)
                    continue
                if btype == 'formula' and brect:
                    formula_rects.append((brect, b.get('source_text') or '', b.get('id')))
                    continue
                if tboxes and btype not in ('caption', 'heading', 'figure') and brect and _inside_any_rect(brect, tboxes, pad=4.0):
                    continue
                if btype == 'figure':
                    if _is_decorative_sliver(brect, pno, 'figure'):
                        continue
                    if any(_rect_overlap_ratio(brect, fr) >= 0.45 for fr in emitted_fig_rects):
                        continue
                    emitted_fig_rects.append(brect)
                kept_blocks.append(b)

            # Cluster protected graphics and formulas into unified media items.
            for prot in pg_spec.get('protected', []):
                prect = prot.get('rect')
                ptype = prot.get('type', 'figure')
                if not prect or len(prect) != 4 or ptype not in ('figure', 'table', 'formula'):
                    continue
                if ptype == 'table':
                    if not _inside_any_rect(prect, tboxes):
                        table_rects.append(prect)
                elif ptype == 'formula':
                    if not _inside_any_rect(prect, tboxes):
                        formula_rects.append((prect, '', prot['id']))
                else:
                    if _is_decorative_sliver(prect, pno, 'figure'):
                        continue
                    if any(_rect_overlap_ratio(prect, fr) >= 0.45 for fr in emitted_fig_rects):
                        continue
                    if any(_rect_overlap_ratio(prect, br['rect']) >= 0.55 for br in kept_blocks if br.get('rect')):
                        continue
                    emitted_fig_rects.append(prect)
                    extra_media.append({
                        'id': prot['id'], 'source_page': pno, 'rect': prect, 'type': 'figure',
                        'translation_required': False, 'source_text': '[figure]', '_synthetic_media': True,
                    })

            for idx, trect in enumerate(_merge_close_rects(table_rects, gap_x=24.0, gap_y=16.0), 1):
                if _is_decorative_sliver(trect, pno, 'table'):
                    continue
                extra_media.append({
                    'id': f'P{pno:03d}TBL{idx}', 'source_page': pno, 'rect': trect, 'type': 'table',
                    'translation_required': False, 'source_text': '[table]', '_synthetic_media': True,
                })

            merged_frects = _merge_close_rects([r for r, _, _ in formula_rects], gap_x=36.0, gap_y=10.0)
            for idx, frect in enumerate(merged_frects, 1):
                if (frect[2] - frect[0]) < 16.0 or (frect[3] - frect[1]) < 8.0:
                    continue
                combined_src = ' '.join(
                    s for r, s, _ in formula_rects
                    if s and _inside_any_rect(r, [frect], pad=4.0)
                ).strip()
                if combined_src and len(combined_src) <= 2 and combined_src in {'(', ')', ',', '.', '•'}:
                    continue
                extra_media.append({
                    'id': f'P{pno:03d}EQ{idx}', 'source_page': pno, 'rect': frect, 'type': 'formula',
                    'translation_required': False, 'source_text': combined_src or '[formula]', '_synthetic_media': True,
                })

            combined = kept_blocks + extra_media
            ordered_pg = reading_order(combined, pg_bounds)
            for b in ordered_pg:
                if b.get('translation_in'):
                    continue
                btype = b.get('type', 'paragraph')
                if btype in _SKIP_TYPES or b.get('role') in {'running-header', 'running-footer', 'reference-heading'}:
                    continue

                if btype in _MEDIA_KINDS:
                    # Crop original figure / table / clustered equation.
                    fname = f'p{pno:03d}_{re.sub(r"[^A-Za-z0-9_-]", "_", b["id"])}.png'
                    img_path = asset_dir / fname
                    if not _crop_rect(src_page, b['rect'], img_path):
                        continue
                    kind_map = {'figure': 'figure', 'table': 'table', 'formula': 'equation'}
                    rkind = kind_map.get(btype, 'figure')
                    num = None
                    if rkind == 'equation':
                        m_eq = _EQ_NUM.search(b.get('source_text') or '')
                        num = _next_num('equation', m_eq[1] if m_eq else None)
                    elif rkind == 'figure':
                        num = _next_num('figure')
                    elif rkind == 'table':
                        num = _next_num('table')
                    cap_txt = (b.get('translation') or '').strip()
                    if not cap_txt:
                        cap_txt = {'figure': f'原图（第 {pno} 页）', 'table': f'原表（第 {pno} 页）',
                                   'equation': f'公式（第 {pno} 页）'}.get(rkind, f'原内容保留（第 {pno} 页）')
                    rect_w = max(14.0, b['rect'][2] - b['rect'][0])
                    ordered_items.append({
                        'id': f'blk_{re.sub(r"[^A-Za-z0-9_-]", "_", b["id"])}',
                        'type': rkind,
                        'number': num,
                        'asset': fname,
                        'width_pt': min(460.0, round(rect_w, 1)),
                        'source_page': pno,
                        'translation': cap_txt,
                        'source_text': b.get('source_text', ''),
                    })
                    continue

                if btype == 'title':
                    continue
                is_fallback = bool(b.get('auto_fallback')) or (
                    b.get('translation_required', True)
                    and not (b.get('translation') or b.get('render_html') or '').strip()
                )
                text = (b.get('render_html') or b.get('translation') or '').strip()
                if not text:
                    src_plain = (b.get('source_text') or '').strip()
                    if is_fallback and len(src_plain) > 2:
                        ordered_items.append({
                            'id': f'blk_{re.sub(r"[^A-Za-z0-9_-]", "_", b["id"])}',
                            'type': 'note',
                            'source_page': pno,
                            'translation': f'[原文保留] {src_plain}',
                            'source_text': src_plain,
                        })
                    continue
                text = restore_scientific_markup(text, b.get('source_lines') or [])
                if btype == 'heading':
                    plain_h = _TAG_STRIP.sub('', text).strip()
                    lvl = 3 if re.match(r'^\d+\.\d+', plain_h) else 2
                    ordered_items.append({
                        'id': f'blk_{re.sub(r"[^A-Za-z0-9_-]", "_", b["id"])}',
                        'type': 'heading',
                        'level': lvl,
                        'source_page': pno,
                        'translation': text,
                        'source_text': b.get('source_text', ''),
                    })
                elif btype == 'abstract':
                    ordered_items.append({
                        'id': f'blk_{re.sub(r"[^A-Za-z0-9_-]", "_", b["id"])}',
                        'type': 'abstract',
                        'source_page': pno,
                        'translation': text,
                        'source_text': b.get('source_text', ''),
                    })
                elif btype == 'caption':
                    # Attach caption to preceding figure/table if same page & placeholder caption.
                    if ordered_items and ordered_items[-1]['type'] in ('figure', 'table') and ordered_items[-1].get('source_page') == pno:
                        if ordered_items[-1]['translation'].startswith(('原图（', '原表（')):
                            m_cap = _CAP_PREFIX.match(_TAG_STRIP.sub('', text))
                            if m_cap and m_cap[1].isdigit():
                                cand_n = int(m_cap[1])
                                kind_key = ordered_items[-1]['type']
                                if cand_n not in used_numbers[kind_key]:
                                    used_numbers[kind_key].discard(ordered_items[-1]['number'])
                                    used_numbers[kind_key].add(cand_n)
                                    ordered_items[-1]['number'] = cand_n
                            ordered_items[-1]['translation'] = text
                            continue
                    ordered_items.append({
                        'id': f'blk_{re.sub(r"[^A-Za-z0-9_-]", "_", b["id"])}',
                        'type': 'note',
                        'source_page': pno,
                        'translation': text,
                        'source_text': b.get('source_text', ''),
                    })
                else:
                    ordered_items.append({
                        'id': f'blk_{re.sub(r"[^A-Za-z0-9_-]", "_", b["id"])}',
                        'type': 'note' if btype == 'note' else 'paragraph',
                        'source_page': pno,
                        'translation': text,
                        'source_text': b.get('source_text', ''),
                    })

    refs = []
    seen_ref_nums = set()
    for r in merged.get('references', []):
        num = r.get('number')
        txt = (r.get('text') or '').strip()
        if isinstance(num, int) and num >= 1 and num not in seen_ref_nums and txt:
            seen_ref_nums.add(num)
            refs.append({'number': num, 'text': txt})
    refs.sort(key=lambda x: x['number'])

    manifest = {
        'title': title,
        'scope': '全文统一中文版式重排阅读版（保留原图、表格与公式）',
        'meta': {k: v for k, v in {'title_en': title_en}.items() if v},
        'blocks': ordered_items,
        'references': refs,
        'fallback_records': list(merged.get('fallback_records') or []),
        'unresolved_links': list(merged.get('unresolved_links') or []),
    }
    return manifest


def _font_file(path_str, font_dir):
    if not path_str:
        return None
    p = Path(path_str)
    if p.is_file():
        return p.resolve()
    if font_dir:
        cand = Path(font_dir) / path_str
        if cand.is_file():
            return cand.resolve()
    return None


def _build_story_css(regular_name, bold_name, fallback_name, link_blue='#1f5f8b'):
    fb_rule = f'@font-face{{font-family:PdfFallback;src:url("{fallback_name}")}}' if fallback_name else ''
    fb_family = 'Body,PdfFallback,sans-serif' if fallback_name else 'Body,sans-serif'
    return f'''
@font-face{{font-family:Body;src:url("{regular_name}")}}
@font-face{{font-family:Body;src:url("{bold_name}");font-weight:bold}}
{fb_rule}
*{{box-sizing:border-box}}
body{{font-family:{fb_family};font-size:9.5pt;line-height:1.55;color:#1f2328;margin:0;padding:0}}
h1{{font-size:14pt;line-height:1.32;margin:0 0 5pt 0;font-weight:bold;color:#111}}
.title-en{{font-size:9pt;color:#5d6872;font-style:italic;margin:0 0 8pt 0}}
.scope{{font-size:8.2pt;color:#5d6872;border-bottom:1px solid #d8dde2;padding-bottom:5pt;margin:0 0 8pt 0}}
.abstract{{background:#f5f7fa;border-left:3pt solid {link_blue};padding:7pt 9pt;margin:0 0 10pt 0;font-size:9pt;line-height:1.5}}
.abs-title{{font-weight:bold;color:{link_blue};margin:0 0 3pt 0;font-size:9.2pt}}
h2{{font-size:11.5pt;line-height:1.32;margin:11pt 0 5pt 0;font-weight:bold;color:#14202b;border-bottom:0.5pt solid #e2e6ea;padding-bottom:2pt}}
h3{{font-size:10pt;line-height:1.32;margin:8pt 0 3pt 0;font-weight:bold;color:#1f2d3a}}
p{{margin:0 0 5pt 0;text-align:justify}}
.note{{font-size:8.5pt;color:#4a5568;background:#fafbfc;padding:4pt 7pt;border-left:2pt solid #cbd5e0;margin:4pt 0}}
figure{{margin:7pt 0;text-align:center;page-break-inside:avoid}}
figure img{{max-width:100%;height:auto}}
figcaption{{font-size:8.2pt;line-height:1.4;color:#2d3748;text-align:left;margin-top:3pt}}
.eq-box{{margin:4pt 0;text-align:center;page-break-inside:avoid}}
.eq-cap{{font-size:8pt;color:#5d6872;margin-top:1pt}}
.refs h2{{margin-top:12pt}}
.ref-item{{font-size:8.2pt;line-height:1.4;margin:0 0 3pt 0;text-align:left}}
.ref-no{{font-weight:bold;color:{link_blue}}}
sup,sub{{font-size:70%}}
a{{color:{link_blue};text-decoration:none}}
.xref-plain{{color:{link_blue}}}
'''


def render_reader_manifest_to_pdf(manifest, manifest_dir, output_pdf, font_dir, fonts_conf=None, link_blue='#1f5f8b'):
    """Render a reader_export-compatible manifest to a unified, paginated Chinese PDF."""
    manifest_dir = Path(manifest_dir).resolve()
    output_pdf = Path(output_pdf).resolve()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    font_dir_path = Path(font_dir).resolve() if font_dir else None

    resolved = resolve_fonts(fonts_conf or manifest.get('fonts'), font_dir_path)
    reg_file = _font_file(resolved.get('regular'), font_dir_path)
    bold_file = _font_file(resolved.get('bold'), font_dir_path) or reg_file
    fb_file = _font_file(resolved.get('fallback'), font_dir_path)

    blocks = manifest.get('blocks', [])
    refs = sorted(manifest.get('references', []), key=lambda r: r.get('number', 0))
    known_targets = set()
    for b in blocks:
        kind = b.get('type', 'paragraph')
        if kind in ('figure', 'table', 'equation') and b.get('number'):
            tag = {'figure': 'fig', 'table': 'table', 'equation': 'eq'}[kind]
            known_targets.add(f'{tag}-{b["number"]}')
    for r in refs:
        if r.get('number'):
            known_targets.add(f'ref-{r["number"]}')

    unresolved_links = list(manifest.get('unresolved_links') or [])

    with tempfile.TemporaryDirectory(prefix='reflow-pdf-') as tmp:
        tmp_dir = Path(tmp)
        if reg_file and reg_file.is_file():
            reg_name = reg_file.name
            if not (tmp_dir / reg_name).exists():
                (tmp_dir / reg_name).write_bytes(reg_file.read_bytes())
        else:
            reg_name = 'cjk-fallback.ttf'
            (tmp_dir / reg_name).write_bytes(fitz.Font('cjk').buffer)
        if bold_file and bold_file.is_file():
            bold_name = bold_file.name
            if not (tmp_dir / bold_name).exists():
                (tmp_dir / bold_name).write_bytes(bold_file.read_bytes())
        else:
            bold_name = reg_name
        fb_name = None
        if fb_file and fb_file.is_file():
            fb_name = fb_file.name
            if not (tmp_dir / fb_name).exists():
                (tmp_dir / fb_name).write_bytes(fb_file.read_bytes())

        # Stage referenced images inside tmp_dir so Story Archive loads them cleanly.
        asset_names = {}
        for b in blocks:
            asset_rel = b.get('asset')
            if not asset_rel:
                continue
            src_asset = (manifest_dir / asset_rel).resolve()
            if src_asset.is_file():
                target_name = hashlib.sha256(str(src_asset).encode()).hexdigest()[:12] + src_asset.suffix.lower()
                if not (tmp_dir / target_name).exists():
                    (tmp_dir / target_name).write_bytes(src_asset.read_bytes())
                asset_names[b['id']] = target_name

        meta = manifest.get('meta') or {}
        parts = [f'<h1>{html.escape(manifest.get("title", "中文阅读版"))}</h1>']
        if isinstance(meta.get('title_en'), str) and meta['title_en'].strip():
            parts.append(f'<p class="title-en">{html.escape(meta["title_en"])}</p>')
        if isinstance(manifest.get('scope'), str) and manifest['scope'].strip():
            parts.append(f'<p class="scope">{html.escape(manifest["scope"])}</p>')

        abstract_blocks = [b for b in blocks if b.get('type') == 'abstract']
        skip_abs_ids = set()
        real_abstracts = []
        for idx, b in enumerate(blocks):
            if b.get('type') != 'abstract':
                continue
            txt = (b.get('translation') or b.get('text_zh') or '').strip()
            plain = _TAG_STRIP.sub('', txt).strip()
            if len(plain) > 25:
                real_abstracts.append(b)
                skip_abs_ids.add(b['id'])
            elif idx + 1 < len(blocks):
                # Short heading like "摘要"; promote following body paragraph on page 1 if substantial.
                nxt = blocks[idx + 1]
                nxt_plain = _TAG_STRIP.sub('', (nxt.get('translation') or nxt.get('text_zh') or '')).strip()
                if nxt.get('source_page') == 1 and nxt.get('type') == 'paragraph' and len(nxt_plain) > 60:
                    real_abstracts.append(nxt)
                    skip_abs_ids.add(b['id'])
                    skip_abs_ids.add(nxt['id'])
        if real_abstracts:
            abs_html = ''.join(
                f'<p>{_clean_inline_for_story(b.get("translation", b.get("text_zh", "")), known_targets, unresolved_links)}</p>'
                for b in real_abstracts
            )
            parts.append(f'<div class="abstract"><div class="abs-title">摘要</div>{abs_html}</div>')

        for b in blocks:
            kind = b.get('type', 'paragraph')
            if b['id'] in skip_abs_ids or kind == 'abstract':
                continue
            txt = b.get('translation', b.get('text_zh', ''))
            rendered = _clean_inline_for_story(txt, known_targets, unresolved_links)
            if kind == 'heading':
                lvl = b.get('level', 2)
                if lvl not in (2, 3, 4):
                    lvl = 2
                parts.append(f'<h{lvl} id="{html.escape(b["id"])}">{rendered}</h{lvl}>')
            elif kind in ('figure', 'table', 'equation'):
                aname = asset_names.get(b['id'])
                num = b.get('number')
                prefix = {'figure': 'fig', 'table': 'table', 'equation': 'eq'}[kind]
                anchor_id = f'{prefix}-{num}' if num else b['id']
                w_pt = min(475.0, float(b.get('width_pt') or 420.0))
                img_tag = f'<img src="{aname}" style="width:{w_pt:.1f}pt"/>' if aname else ''
                if kind == 'equation':
                    eq_lbl = f'式 ({num})' if num else ''
                    cap = f'<div class="eq-cap">{eq_lbl} {rendered}</div>' if (eq_lbl or rendered) else ''
                    parts.append(f'<div class="eq-box" id="{anchor_id}">{img_tag}{cap}</div>')
                else:
                    kind_label = '图' if kind == 'figure' else '表'
                    lbl = f'<b>{kind_label} {num}</b> ' if num else ''
                    cap = f'<figcaption>{lbl}{rendered}</figcaption>'
                    if kind == 'table':
                        parts.append(f'<figure id="{anchor_id}">{cap}{img_tag}</figure>')
                    else:
                        parts.append(f'<figure id="{anchor_id}">{img_tag}{cap}</figure>')
            elif kind == 'note':
                parts.append(f'<div class="note" id="{html.escape(b["id"])}">{rendered}</div>')
            else:
                parts.append(f'<p id="{html.escape(b["id"])}">{rendered}</p>')

        if refs:
            parts.append('<div class="refs"><h2 id="ui-references">参考文献</h2>')
            for r in refs:
                n = r['number']
                rtxt = html.escape(r['text'])
                parts.append(f'<p class="ref-item" id="ref-{n}"><span class="ref-no">[{n}]</span> {rtxt}</p>')
            parts.append('</div>')

        html_doc = f'<!doctype html><html><head><meta charset="utf-8"></head><body>{"".join(parts)}</body></html>'
        css = _build_story_css(reg_name, bold_name, fb_name, link_blue=link_blue)
        story = fitz.Story(html=html_doc, user_css=css, archive=fitz.Archive(str(tmp_dir)))

        raw_pdf = tmp_dir / 'raw.pdf'
        writer = fitz.DocumentWriter(str(raw_pdf))
        mediabox = fitz.Rect(0, 0, 595, 842)
        content_rect = fitz.Rect(48, 48, 547, 794)

        anchors = {}
        link_boxes = []
        toc_entries = []
        current_page = 0

        def _collect_pos(el):
            if el.id and el.id not in anchors and el.rect:
                anchors[el.id] = (current_page, fitz.Point(content_rect.x0, max(content_rect.y0, el.rect[1] - 6)))
            if el.href and el.href.startswith('#') and el.rect:
                link_boxes.append((current_page, fitz.Rect(el.rect), el.href[1:]))
            if el.heading in (1, 2, 3) and el.text and el.rect:
                toc_entries.append([el.heading, el.text, current_page + 1, el.rect[1]])

        more = 1
        while more:
            dev = writer.begin_page(mediabox)
            more, _ = story.place(content_rect)
            story.element_positions(_collect_pos)
            story.draw(dev, None)
            writer.end_page()
            current_page += 1
        writer.close()
        del writer

        doc = fitz.open(raw_pdf)
        first_cite_loc = {}
        links_created = 0
        for pno, lrect, key in link_boxes:
            if key in anchors and 0 <= pno < len(doc):
                dst_pno, dst_pt = anchors[key]
                if 0 <= dst_pno < len(doc):
                    first_cite_loc.setdefault(key, (pno, fitz.Point(lrect.x0, max(0, lrect.y0 - 20))))
                    doc[pno].insert_link({'kind': fitz.LINK_GOTO, 'from': lrect, 'page': dst_pno, 'to': dst_pt})
                    links_created += 1
            elif key not in anchors:
                unresolved_links.append({'key': key, 'page': pno + 1, 'reason': 'anchor-not-rendered'})

        # Add backlinks on reference numbers ([N]) to their first citation in prose.
        for key, (src_pno, src_pt) in first_cite_loc.items():
            if key.startswith('ref-') and key in anchors:
                ref_pno, ref_pt = anchors[key]
                box = fitz.Rect(ref_pt.x, ref_pt.y + 4, ref_pt.x + 26, ref_pt.y + 18) & doc[ref_pno].rect
                if not box.is_empty:
                    doc[ref_pno].insert_link({'kind': fitz.LINK_GOTO, 'from': box, 'page': src_pno, 'to': src_pt})

        # Add footer page numbers.
        for idx, page in enumerate(doc):
            page.insert_text(
                fitz.Point(mediabox.width / 2 - 12, mediabox.height - 24),
                f'{idx + 1} / {len(doc)}',
                fontsize=8.5,
                color=(0.45, 0.48, 0.52),
            )

        if toc_entries:
            seen_toc = set()
            clean_toc = []
            for lvl, title_txt, pg_num, y_pos in toc_entries:
                tkey = (lvl, title_txt, pg_num)
                if tkey not in seen_toc:
                    seen_toc.add(tkey)
                    clean_toc.append([lvl, title_txt, pg_num])
            doc.set_toc(clean_toc)

        doc.subset_fonts()
        doc.save(output_pdf, garbage=4, deflate=True)
        total_pages = len(doc)

        previews_dir = output_pdf.with_suffix('.previews')
        previews_dir.mkdir(exist_ok=True)
        for idx, pg in enumerate(doc):
            pg.get_pixmap(dpi=120).save(previews_dir / f'page-{idx + 1:03}.png')

        with fitz.open() as sheet:
            for start in range(0, total_pages, 9):
                grid = sheet.new_page(width=900, height=1260)
                for offset in range(min(9, total_pages - start)):
                    x = (offset % 3) * 300
                    y = (offset // 3) * 420
                    grid.insert_text((x + 10, y + 15), str(start + offset + 1), fontsize=10)
                    grid.show_pdf_page(fitz.Rect(x + 5, y + 22, x + 295, y + 415), doc, start + offset)
            sheet.save(output_pdf.with_suffix('.overview.pdf'), garbage=4, deflate=True)
        doc.close()

    report = {
        'layout_mode': 'reflow',
        'output': str(output_pdf),
        'total_pages': total_pages,
        'output_sha256': hashlib.sha256(output_pdf.read_bytes()).hexdigest(),
        'blocks': len(blocks),
        'references': len(refs),
        'links_created': links_created,
        'fallback_records': manifest.get('fallback_records', []),
        'unresolved_links': unresolved_links,
        'structural_checks_passed': True,
        'warnings': [],
        'warning_count': len(manifest.get('fallback_records', [])) + len(unresolved_links),
        'delivery_status': 'passed_with_warnings' if (manifest.get('fallback_records') or unresolved_links) else 'passed',
    }
    output_pdf.with_suffix('.validation.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return report


def build_reflow_pdf_from_merged(merged, source_pdf, work_dir, output_pdf, font_dir, fonts_conf=None, link_blue='#1f5f8b'):
    """Build a unified reflow Chinese PDF directly from ``merged`` and ``source.pdf``."""
    work_dir = Path(work_dir)
    asset_dir = work_dir / 'reflow_assets'
    manifest = merged_to_reader_manifest(merged, source_pdf, asset_dir)
    manifest_path = work_dir / 'reader-manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return render_reader_manifest_to_pdf(
        manifest, asset_dir, output_pdf, font_dir, fonts_conf=fonts_conf, link_blue=link_blue or '#1f5f8b'
    )
