"""Link split physical boxes into one logical paragraph and pour the translation back.

MinerU text blocks group fragments when an extraction directory is present.
Otherwise geometry links a sentence that continues across a line, a column or a
page. Placeholders and glossary checks run on the merged source. Rendering then
streams that one translation through the physical boxes in order, in proportion
to box area, and never cuts an inline ``sup``/``sub``/``i``/``a`` tag.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .classifier import adapted_body_size, intersects


_TAG = re.compile(r'</?(?:sup|sub|i|b|a)\b[^>]*>', re.I)
_ATOMIC = re.compile(
    r'(?is)(<a\b[^>]*>.*?</a>|[A-Za-z0-9@._+-]+(?:\s*<(?:sup|sub)\b[^>]*>.*?</(?:sup|sub)>[A-Za-z0-9._+-]*)*|<sup\b[^>]*>.*?</sup>|<sub\b[^>]*>.*?</sub>|<i\b[^>]*>.*?</i>|<[^>]+>)')
_HEADING = re.compile(
    r'^(?:\d+(?:\.\d+)*[.)]?\s*)?(?:introduction|experimental|experiments|results|discussion|'
    r'conclusion|conclusions|methods|materials|highlights|keywords|abstract|references|'
    r'acknowledgements|nomenclature)\b', re.I)
_SENTENCE_END = re.compile(r'[.!?。！？]["\'\)\]]*$')


def plain_text(value):
    return re.sub(r'\s+', ' ', _TAG.sub('', value or '')).strip()


def _compact(value):
    return re.sub(r'\s+', '', plain_text(value)).lower()


def _is_heading_text(text):
    plain = plain_text(text)
    if not plain or len(plain) > 80:
        return False
    if _HEADING.match(plain):
        return True
    if re.match(r'^\d+(?:\.\d+)+\s+[A-Z]', plain) and not _SENTENCE_END.search(plain):
        return True
    letters = re.sub(r'[^A-Za-z]', '', plain)
    return bool(letters and letters.upper() == letters and re.search(r'[A-Z]{3,}', plain) and len(plain.split()) <= 8)


def prose_member(block):
    return (block.get('translation_required', True)
            and block.get('type') in {'paragraph', 'abstract', 'caption'}
            and not block.get('bold')
            and not _is_heading_text(block.get('source_text', ''))
            and block.get('role') not in {'metadata', 'reference-heading', 'post-reference-heading'})


def _open_sentence(text):
    plain = plain_text(text)
    return bool(plain) and not _SENTENCE_END.search(plain)


def _continues_lexically(nxt):
    plain = plain_text(nxt.get('source_text', ''))
    if not plain:
        return False
    if nxt.get('role') == 'inline-fragment':
        return True
    if re.match(r'^\([A-Za-z0-9+\-]{1,4}\)\s+[a-z]', plain):
        return True
    first = plain.lstrip('([{"\'')
    if not first:
        return False
    # A new capitalized sentence is not the tail of the previous one.
    return not first[0].isupper()


def _column(rect, bounds):
    mid = (bounds[0] + bounds[2]) / 2
    if rect[2] <= mid + 4:
        return 'left'
    if rect[0] >= mid - 4:
        return 'right'
    return 'full'


def continues(prev, nxt, page_bounds):
    """True when ``nxt`` is the geometric tail of an unfinished sentence."""
    if not prose_member(prev) or not prose_member(nxt):
        return False
    if not _open_sentence(prev.get('source_text', '')) or not _continues_lexically(nxt):
        return False
    if prev.get('type') != nxt.get('type'):
        return False
    ps = float(prev.get('source_font_pt') or prev.get('size') or 9)
    ns = float(nxt.get('source_font_pt') or nxt.get('size') or 9)
    if abs(ps - ns) > 1.5:
        return False
    if prev['source_page'] == nxt['source_page'] and intersects(prev['rect'], nxt['rect']):
        if prev.get('type') == 'caption' and nxt.get('type') == 'caption' and abs(prev['rect'][0] - nxt['rect'][0]) <= 4:
            return True
        # Overlapping fragments share one typeset region; linking is decided by
        # the caller only when they are inline pieces of the same sentence.
        return nxt.get('role') == 'inline-fragment' or prev.get('role') == 'inline-fragment'
    bounds = page_bounds.get(prev['source_page'])
    if not bounds:
        return False
    if prev['source_page'] == nxt['source_page']:
        pc, nc = _column(prev['rect'], bounds), _column(nxt['rect'], bounds)
        gap_y = nxt['rect'][1] - prev['rect'][3]
        gap_x = nxt['rect'][0] - prev['rect'][2]
        same_baseline = abs((prev['rect'][1] + prev['rect'][3]) / 2 - (nxt['rect'][1] + nxt['rect'][3]) / 2) <= max(ps, ns)
        if pc == nc and same_baseline and -2 <= gap_x <= ps * 8:
            return True
        if (pc == nc and (pc != 'full' or prev.get('type') == 'caption')
                and -12 <= gap_y <= max(ps * 0.85, 8) and abs(prev['rect'][0] - nxt['rect'][0]) <= ps * 4):
            return True
        height = bounds[3] - bounds[1]
        if pc == 'left' and nc == 'right' and prev['rect'][3] > bounds[1] + height * 0.65 and nxt['rect'][1] < bounds[1] + height * 0.72:
            return True
        return False
    if nxt['source_page'] != prev['source_page'] + 1:
        return False
    nxt_bounds = page_bounds.get(nxt['source_page'])
    if not nxt_bounds:
        return False
    height = bounds[3] - bounds[1]
    nxt_height = nxt_bounds[3] - nxt_bounds[1]
    pc, nc = _column(prev['rect'], bounds), _column(nxt['rect'], nxt_bounds)
    low = prev['rect'][3] > bounds[1] + height * 0.65
    high = nxt['rect'][1] < nxt_bounds[1] + nxt_height * 0.88
    return bool(low and high and (pc == nc or {pc, nc} <= {'left', 'right', 'full'}))


def merge_source(parts):
    """Join physical fragments, healing a hyphen that exists only because of a line break."""
    out = []
    for part in parts:
        piece = (part or '').strip()
        if not piece:
            continue
        if out and out[-1].endswith('-') and piece[:1].islower():
            out[-1] = out[-1][:-1] + piece
        elif out:
            out.append(' ')
            out.append(piece)
        else:
            out.append(piece)
    return ''.join(out)


def _link(members):
    members = [b for b in members if prose_member(b)]
    if len(members) < 2:
        return
    gid = 'lp-' + members[0]['id']
    merged = merge_source(b.get('source_text', '') for b in members)
    sources = [float(b.get('source_font_pt') or b.get('size') or 9) for b in members]
    size = min(float(b.get('size') or 8) for b in members) if members[0].get('type') == 'caption' else adapted_body_size(min(sources))
    leading = float(members[0].get('leading') or 1.17)
    for index, block in enumerate(members):
        block['logical_paragraph_id'] = gid
        block['logical_index'] = index
        block['size'] = size
        block['leading'] = leading
        block['min_scale'] = 0.85
        if index == 0:
            block['logical_source'] = merged
            block.pop('translation_in', None)
            block['join_previous'] = False
        else:
            block['translation_in'] = members[0]['id']
            block['join_previous'] = True


def _denorm(bbox, bounds):
    if max(bbox) <= 1000:
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        return [bounds[0] + bbox[0] * width / 1000, bounds[1] + bbox[1] * height / 1000,
                bounds[0] + bbox[2] * width / 1000, bounds[1] + bbox[3] * height / 1000]
    return list(bbox)


def _center_in(rect, box):
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _overlap_frac(rect, box):
    w = min(rect[2], box[2]) - max(rect[0], box[0])
    h = min(rect[3], box[3]) - max(rect[1], box[1])
    area = max(rect[2] - rect[0], 0.01) * max(rect[3] - rect[1], 0.01)
    if w <= 0 or h <= 0:
        return 0
    return w * h / area


def load_mineru_items(extraction_dir):
    """Read a MinerU content list from ``work/extraction`` without calling the network."""
    root = Path(extraction_dir)
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.rglob('*.json')):
        if path.name in {'extraction-report.json', 'source.json'}:
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        items = data if isinstance(data, list) else None
        if items is None and isinstance(data, dict):
            items = data.get('content_list') or data.get('pdf_info')
        if isinstance(items, list) and any(isinstance(item, dict) and item.get('text') for item in items):
            found.extend(item for item in items if isinstance(item, dict))
    if found:
        return found
    paragraphs = []
    for path in sorted(root.rglob('*.md')):
        try:
            text = path.read_text(encoding='utf-8-sig')
        except OSError:
            continue
        for chunk in re.split(r'\n\s*\n', text):
            chunk = chunk.strip()
            if chunk:
                paragraphs.append({'type': 'text', 'text': chunk})
    return paragraphs


def _mineru_groups(blocks, items, pages):
    bounds = {p['source_page']: p['rect'] for p in pages}
    groups = []
    claimed = set()
    text_items = [item for item in items
                  if item.get('type') in {None, 'text', 'title'} or (item.get('text') and item.get('bbox'))]
    for item in text_items:
        if item.get('type') in {'image', 'table', 'equation', 'header', 'footer', 'page_number', 'page_footnote'}:
            continue
        blob = _compact(item.get('text', ''))
        if not blob:
            continue
        bbox, page = item.get('bbox'), item.get('page_idx')
        hits = []
        if isinstance(bbox, list) and len(bbox) == 4 and isinstance(page, int) and page + 1 in bounds:
            box = _denorm(bbox, bounds[page + 1])
            for block in blocks:
                if block['id'] in claimed or block.get('source_page') != page + 1 or not prose_member(block):
                    continue
                if _center_in(block['rect'], box) or _overlap_frac(block['rect'], box) >= 0.45:
                    piece = _compact(block.get('source_text', ''))
                    if piece and piece in blob:
                        hits.append(block)
        else:
            # Markdown-only extraction: consume the next prose blocks whose
            # concatenation is a prefix of this paragraph.
            acc = ''
            for block in blocks:
                if block['id'] in claimed or not prose_member(block):
                    continue
                piece = _compact(block.get('source_text', ''))
                if not piece or not blob.startswith(acc + piece):
                    if hits:
                        break
                    continue
                hits.append(block)
                acc += piece
                if acc == blob:
                    break
            if acc and len(acc) < len(blob) * 0.8:
                hits = []
        if len(hits) >= 2:
            hits.sort(key=lambda b: (b.get('source_page', 0), b['rect'][1], b['rect'][0], b['id']))
            groups.append(hits)
            claimed.update(b['id'] for b in hits)
    return groups


def link_paragraphs(mapping, extraction_dir=None):
    """Attach ``logical_paragraph_id`` / ``translation_in`` / ``join_previous``.

    MinerU grouping wins when an extraction is available. Geometric continuation
    links whatever MinerU did not claim, and is the only linker otherwise.
    """
    from .classifier import reading_order
    from .structure import source_order
    order = source_order(mapping)
    rank = {ident: index for index, ident in enumerate(order)}
    page_rects = {p['source_page']: p['rect'] for p in mapping.get('pages', [])}
    full_rank = {}
    cursor = 0
    for pn in sorted(page_rects):
        page_blocks = [b for b in mapping.get('blocks', []) if b.get('source_page') == pn]
        for b in reading_order(page_blocks, page_rects[pn]):
            full_rank[b['id']] = cursor
            cursor += 1
    blocks = sorted(mapping.get('blocks', []), key=lambda b: (b.get('source_page', 0), full_rank.get(b['id'], rank.get(b['id'], 10**9))))
    page_bounds = {p['source_page']: p['rect'] for p in mapping.get('pages', [])}
    assigned = set()
    if extraction_dir:
        for group in _mineru_groups(blocks, load_mineru_items(extraction_dir), mapping.get('pages', [])):
            _link(group)
            assigned.update(b['id'] for b in group)
    current = []
    cap_current = []

    def flush():
        if len(current) >= 2:
            _link(list(current))
        current.clear()

    def flush_cap():
        if len(cap_current) >= 2:
            _link(list(cap_current))
        cap_current.clear()

    for block in blocks:
        if block['id'] in assigned:
            flush()
            flush_cap()
            continue
        if block.get('type') == 'caption' and prose_member(block):
            if cap_current and not continues(cap_current[-1], block, page_bounds):
                flush_cap()
            cap_current.append(block)
            continue
        flush_cap()
        if not prose_member(block):
            if block.get('type') in {'header', 'footer', 'figure', 'caption', 'table'} or block.get('role') == 'metadata':
                # Floating figures, tables, captions and running headers/footers
                # sit between columns or pages without ending the running sentence.
                continue
            flush()
            continue
        if (current and _open_sentence(current[-1].get('source_text', ''))
                and (re.match(r'^(?:abbreviations|footnotes?|notes?)\s*:|^(?:fig(?:ure)?\.?|table)\s*\d+\b', plain_text(block.get('source_text', '')), re.I)
                     or (float(block.get('source_font_pt') or 9) <= 7.5 and float(current[-1].get('source_font_pt') or 9) >= 8.5))):
            continue
        if current and not continues(current[-1], block, page_bounds):
            flush()
        current.append(block)
    flush()
    flush_cap()
    return mapping


def _pieces(html):
    parts = []
    cursor = 0
    for match in _ATOMIC.finditer(html or ''):
        if match.start() > cursor:
            parts.extend(html[cursor:match.start()])
        parts.append(match.group())
        cursor = match.end()
    if cursor < len(html or ''):
        parts.extend(html[cursor:])
    return parts


def _visible_len(fragment):
    return len(re.sub(r'<[^>]+>', '', fragment))


def split_flow(html, weights):
    """Split ``html`` across weights at character boundaries, never inside protected tags.

    Returns ``None`` when the text cannot be divided into that many nonempty
    pieces without cutting a tag. Concatenating a successful result restores
    ``html`` exactly.
    """
    html = html or ''
    count = len(weights)
    if count <= 1:
        return [html]
    pending, grouped = [], []
    for piece in _pieces(html):
        pending.append(piece)
        if _visible_len(piece):
            grouped.append(''.join(pending))
            pending = []
    if pending:
        if grouped:
            grouped[-1] += ''.join(pending)
        else:
            grouped.append(''.join(pending))
    if len(grouped) < count:
        return None
    total = sum(_visible_len(piece) for piece in grouped) or 1
    total_weight = sum(max(weight, 0.01) for weight in weights) or 1
    targets = [max(weight, 0.01) / total_weight * total for weight in weights]
    slices = [[] for _ in weights]
    index = 0
    filled = 0
    for position, piece in enumerate(grouped):
        slices[index].append(piece)
        filled += _visible_len(piece)
        remaining_pieces = len(grouped) - position - 1
        remaining_boxes = count - index - 1
        if remaining_boxes and remaining_pieces >= remaining_boxes and filled >= targets[index] - 0.25:
            index += 1
            filled = 0
    for index in range(count - 1, 0, -1):
        if slices[index]:
            continue
        donor = next((i for i in range(index - 1, -1, -1) if len(slices[i]) >= 2), None)
        if donor is None:
            return None
        slices[index].insert(0, slices[donor].pop())
    rendered = [''.join(slice_) for slice_ in slices]
    if any(not _visible_len(part) for part in rendered) or ''.join(rendered) != html:
        return None
    return rendered


def _area(rect):
    return max(rect[2] - rect[0], 1) * max(rect[3] - rect[1], 1)


def _same_page_overlap(a, b):
    if a.get('source_page') != b.get('source_page'):
        return False
    return intersects(a['rect'], b['rect'])


def prepare_render_text(mapping):
    """Flow each logical translation across its physical boxes and lock one size.

    Overlapping members share the keeper's region (their rectangles are still
    erased). Separated members receive successive slices. Every member of a
    paragraph is given the same ``size``, ``leading`` and ``min_scale``.
    """
    from .structure import source_order
    order = source_order(mapping)
    rank = {ident: index for index, ident in enumerate(order)}
    groups = {}
    for block in mapping.get('blocks', []):
        gid = block.get('logical_paragraph_id')
        if gid:
            groups.setdefault(gid, []).append(block)
    for members in groups.values():
        members.sort(key=lambda b: (b.get('source_page', 0), rank.get(b['id'], 10**9)))
        anchor = next((b for b in members if not b.get('translation_in')), members[0])
        full = anchor.get('logical_translation') or anchor.get('translation') or ''
        for block in members:
            block.pop('absorbed', None)
            block.pop('absorbs', None)
            block.pop('absorb_rects', None)
            block.pop('render_html', None)
        anchor['logical_translation'] = full
        sources = [float(b.get('source_font_pt') or b.get('size') or 9) for b in members]
        size = adapted_body_size(min(sources))
        leading = float(anchor.get('leading') or 1.17)
        for block in members:
            block['size'] = size
            block['leading'] = leading
            block['min_scale'] = 0.85
        keepers = []
        for block in members:
            host = next((keeper for keeper in keepers if _same_page_overlap(keeper, block)), None)
            if host is None:
                keepers.append(block)
                continue
            host.setdefault('absorbs', []).append(block['id'])
            host.setdefault('absorb_rects', []).append(list(block['rect']))
            block['absorbed'] = True
        parts = split_flow(full, [_area(keeper['rect']) for keeper in keepers]) if full.strip() else None
        if parts is None:
            if keepers:
                keepers[0]['render_html'] = full
                for extra in keepers[1:]:
                    keepers[0].setdefault('absorbs', []).append(extra['id'])
                    keepers[0].setdefault('absorb_rects', []).append(list(extra['rect']))
                    extra['absorbed'] = True
                    extra['render_html'] = None
        else:
            for keeper, part in zip(keepers, parts):
                keeper['render_html'] = part
    return mapping
