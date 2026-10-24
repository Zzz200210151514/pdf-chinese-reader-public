"""ACS stacked selectivity symbols: two raised charges over S, with ion scripts.

ACS often draws S^{Na+}_{Ca^{2+}} (and the chloride/sulfate twin) as separate
positioned glyphs. Extraction then emits each baseline as its own line, the
host keeps the literal plus signs, and a later region merge shrinks the
paragraph to the superscript size. This module rebuilds one math block per
symbol before classification, and keeps those script fragments out of prose.
"""
from __future__ import annotations

import html
import re
from collections import Counter


# Custom-font ToUnicode maps used for instrument model digits (CHI 760E).
# U+F6B1 is 0 … U+F6BA is 9, so U+F6B7 is 6. Same map the text validator uses.
_PUA_DIGITS = str.maketrans({0xF6B1 + i: str(i) for i in range(10)})
_ION = re.compile(r'(?:Ca|Na|Cl|SO)')
_SCRIPT_TEXT = re.compile(
    r'^(?:Ca|Na2|Na|Cl|SO4|SO|[0-9]{1,2}|2\+|2-|2−|\+|−|-)+$')
_TAG = re.compile(r'</?(?:sup|sub|i|b|a)\b[^>]*>', re.I)


def apply_pua_digits(blocks):
    """Map private-use instrument digits to ASCII. Idempotent on real digits."""
    for block in blocks or []:
        for line in block.get('lines') or []:
            for span in line.get('spans') or []:
                text = span.get('text')
                if isinstance(text, str) and text:
                    span['text'] = text.translate(_PUA_DIGITS)
                for char in span.get('chars') or []:
                    value = char.get('c')
                    if isinstance(value, str) and value:
                        char['c'] = value.translate(_PUA_DIGITS)
    return blocks


def visible_chars(text):
    """Characters that count as source text, ignoring script tags and space."""
    return re.sub(r'\s+', '', _TAG.sub('', text or ''))


def same_visible_chars(left, right):
    return Counter(visible_chars(left)) == Counter(visible_chars(right))


def is_stacked_script_fragment(text):
    """True for a selectivity charge, ion script, or stacked S, not a sentence.

    Bare citation digits stay prose so an inline ``2`` can still join its line.
    A digit is a fragment only when it already carries a charge or a script tag
    (the ``4`` / ``<sub>2</sub>`` pieces cut off SO4).
    """
    raw = text or ''
    plain = _TAG.sub('', raw).replace('＋', '+').replace('−', '-').replace('–', '-')
    compact = re.sub(r'\s+', '', plain)
    if not compact or len(compact) > 20 or re.search(r'[a-z]{3,}', compact):
        return False
    if re.fullmatch(r'\++', compact) or compact == 'S':
        return True
    if re.fullmatch(r'(?:Ca|Na2|Na|Cl|SO4|SO)', compact):
        return True
    if re.fullmatch(r'[0-9]{1,2}[+-]', compact):
        return True
    tagged = bool(re.search(r'</?(?:sup|sub)\b', raw, re.I))
    if tagged and re.fullmatch(r'[0-9SCaNalO+\-,.]+', compact):
        return True
    if 'S' in compact and re.fullmatch(r'[SCaNalO0-9+\-,.]+', compact) and _ION.search(compact):
        return True
    return False


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _origin_y(span):
    origin = span.get('origin')
    if origin and len(origin) > 1:
        try:
            return float(origin[1])
        except (TypeError, ValueError):
            pass
    box = span.get('bbox') or [0, 0, 0, 0]
    return float(box[3])


def _size(span, default=9.0):
    try:
        size = float(span.get('size') or 0)
    except (TypeError, ValueError):
        return default
    return size or default


def _plain(span):
    return re.sub(r'\s+', '', span.get('text') or '').replace('＋', '+').replace('−', '-').replace('–', '-')


def _is_s_base(span):
    return _plain(span) == 'S'


def _is_plus(span):
    text = _plain(span)
    return bool(text) and set(text) <= {'+'}


def _plus_count(span):
    return _plain(span).count('+')


def _is_script_piece(span):
    text = _plain(span)
    if not text or _is_s_base(span) or _is_plus(span):
        return False
    if re.search(r'[a-z]{2,}', text):
        return False
    return bool(_SCRIPT_TEXT.fullmatch(text))


def _center(span):
    box = span.get('bbox') or [0, 0, 0, 0]
    return (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2


def _raised_plus(plus, base):
    """A charge stacked above this S, not an inline plus on the same baseline."""
    if not _is_plus(plus):
        return False
    bsize = _size(base)
    psize = _size(plus, bsize)
    delta = _origin_y(base) - _origin_y(plus)
    if delta < max(0.35 * bsize, 1.2) or delta > bsize * 3.6:
        return False
    if psize > bsize * 0.92 and not (int(plus.get('flags') or 0) & 1):
        return False
    px, _py = _center(plus)
    box = base.get('bbox') or [0, 0, 0, 0]
    return float(box[0]) - bsize <= px <= float(box[2]) + bsize * 6


def _in_band(span, base, reach):
    bsize = _size(base)
    box = base.get('bbox') or [0, 0, 0, 0]
    cx, cy = _center(span)
    return (float(box[0]) - bsize <= cx <= float(box[2]) + reach
            and float(box[1]) - bsize * 2.4 <= cy <= float(box[3]) + bsize * 1.7)


def _has_ion(spans):
    return any(_ION.search(_plain(span)) for span in spans)


def _iter_spans(blocks):
    found = []
    for bi, block in enumerate(blocks or []):
        if block.get('type') == 1 or block.get('stacked_selectivity'):
            continue
        for li, line in enumerate(block.get('lines') or []):
            for si, span in enumerate(line.get('spans') or []):
                if (span.get('text') or '').strip() and span.get('bbox'):
                    found.append((bi, li, si, span))
    return found


def _nearest_base(span, bases):
    cx, _cy = _center(span)
    best, distance = None, None
    for base in bases:
        bx, _by = _center(base)
        gap = abs(cx - bx)
        if distance is None or gap < distance:
            best, distance = base, gap
    return best


def _nearest_raised_base(plus, bases):
    raised = [base for base in bases if _raised_plus(plus, base)]
    if not raised:
        return None
    return _nearest_base(plus, raised)


def _collect_clusters(indexed):
    """One cluster per selectivity S. A twin S beside a charged S joins too."""
    bases = [span for _bi, _li, _si, span in indexed if _is_s_base(span)]
    pluses = [span for _bi, _li, _si, span in indexed if _is_plus(span)]
    pieces = [span for _bi, _li, _si, span in indexed if _is_script_piece(span)]
    claimed = set()
    clusters = []
    seeded = []
    for base in sorted(bases, key=lambda span: span['bbox'][0]):
        if id(base) in claimed:
            continue
        owned = [plus for plus in pluses
                 if id(plus) not in claimed and _nearest_raised_base(plus, bases) is base]
        if sum(_plus_count(plus) for plus in owned) < 2:
            continue
        band = [base, *seeded]
        ions = [piece for piece in pieces
                if id(piece) not in claimed and _in_band(piece, base, _size(base) * 6)
                and _nearest_base(piece, [item for item in band if _in_band(piece, item, _size(item) * 6)] or [base]) is base]
        if not _has_ion(ions):
            continue
        cluster = [base, *owned, *ions]
        claimed.update(id(span) for span in cluster)
        seeded.append(base)
        clusters.append(cluster)
    # The chloride/sulfate twin often carries minus charges rather than two
    # pluses. Synthesize it on its own when it sits beside a symbol already built.
    for base in sorted(bases, key=lambda span: span['bbox'][0]):
        if id(base) in claimed or not seeded:
            continue
        if not any(_in_band(base, seed, _size(seed) * 10) for seed in seeded):
            continue
        hosts = [base, *seeded]
        ions = [piece for piece in pieces
                if id(piece) not in claimed and _in_band(piece, base, _size(base) * 6)
                and _nearest_base(piece, [item for item in hosts if _in_band(piece, item, _size(item) * 6)] or [base]) is base]
        if not _has_ion(ions):
            continue
        cluster = [base, *ions]
        claimed.update(id(span) for span in cluster)
        clusters.append(cluster)
    return clusters


def _column_markup(spans):
    """Nest a charge that sits above the ion token in the same column."""
    ordered = sorted(spans, key=lambda span: (_origin_y(span), span['bbox'][0]))
    if len(ordered) == 1:
        return html.escape(ordered[0].get('text') or '', quote=False)
    top = ordered[0]
    rest = ordered[1:]
    body = ''.join(html.escape(span.get('text') or '', quote=False) for span in rest)
    if _is_plus(top) and _origin_y(top) < _origin_y(rest[0]) - 0.4:
        return body + '<sup>' + html.escape(top.get('text') or '', quote=False) + '</sup>'
    return html.escape(top.get('text') or '', quote=False) + body


def _side_markup(spans):
    columns = []
    for span in sorted(spans, key=lambda item: item['bbox'][0]):
        box = span['bbox']
        target = None
        for column in columns:
            if min(column['bbox'][2], box[2]) > max(column['bbox'][0], box[0]) - 1:
                target = column
                break
        if target is None:
            columns.append({'spans': [span], 'bbox': list(box)})
        else:
            target['spans'].append(span)
            target['bbox'] = _union([target['bbox'], box])
    return ''.join(_column_markup(column['spans']) for column in columns)


def stacked_markup(cluster):
    """Stacked ``S<sup>…</sup><sub>…</sub>`` using only characters present in the glyphs."""
    base = next(span for span in cluster if _is_s_base(span))
    others = [span for span in cluster if span is not base]
    sup, sub = [], []
    baseline = _origin_y(base)
    limit = max(0.3 * _size(base), 1.0)
    for span in others:
        delta = _origin_y(span) - baseline
        if delta < -limit:
            sup.append(span)
        else:
            sub.append(span)
    markup = 'S'
    if sup:
        markup += '<sup>' + _side_markup(sup) + '</sup>'
    if sub:
        markup += '<sub>' + _side_markup(sub) + '</sub>'
    if not same_visible_chars(markup, ''.join(span.get('text') or '' for span in cluster)):
        # Geometry could not be nested without dropping or inventing a glyph.
        # Keep every original character inside one explicit script wrapper.
        ordered = sorted(others, key=lambda span: (span['bbox'][0], _origin_y(span)))
        markup = 'S' + ''.join(
            '<sup>' + html.escape(span.get('text') or '', quote=False) + '</sup>'
            if _origin_y(span) < baseline else
            '<sub>' + html.escape(span.get('text') or '', quote=False) + '</sub>'
            for span in ordered)
    return markup


def _formula_block(cluster):
    spans = sorted(cluster, key=lambda span: (_origin_y(span), span['bbox'][0]))
    lines = []
    for span in spans:
        y = round(_origin_y(span), 1)
        if lines and abs(lines[-1]['_y'] - y) <= 0.8:
            lines[-1]['spans'].append(span)
            lines[-1]['bbox'] = _union([lines[-1]['bbox'], span['bbox']])
        else:
            lines.append({'bbox': list(span['bbox']), 'dir': [1, 0], 'wmode': 0,
                          'spans': [span], '_y': y})
    for line in lines:
        line.pop('_y', None)
    bbox = _union([span['bbox'] for span in spans])
    return {'type': 0, 'bbox': bbox, 'lines': lines,
            'stacked_selectivity': True, 'stacked_markup': stacked_markup(cluster)}


def _y_overlap(a, b):
    return min(a[3], b[3]) - max(a[1], b[1])


def _x_overlap(a, b):
    return min(a[2], b[2]) - max(a[0], b[0])


def _line_from(template, spans):
    bbox = _union([span['bbox'] for span in spans])
    line = {key: value for key, value in template.items() if key != 'spans'}
    line['spans'] = spans
    line['bbox'] = bbox
    return line


def _split_around(line, formulas):
    """Keep words on either side of a symbol from covering it with one union box."""
    overlapping = [formula for formula in formulas if _y_overlap(line.get('bbox') or formula, formula) > 0.8]
    if not overlapping:
        return [line]
    left, right = [], []
    for span in line.get('spans') or []:
        box = span.get('bbox')
        if not box:
            continue
        cx = (box[0] + box[2]) / 2
        if any(cx < formula[0] and _y_overlap(box, formula) > -_size(span) for formula in overlapping):
            left.append(span)
        elif any(cx > formula[2] for formula in overlapping):
            right.append(span)
        elif any(_x_overlap(box, formula) > 0 and _y_overlap(box, formula) > 0 for formula in overlapping):
            # A glyph we failed to claim stays put; the caller still splits the line
            # so the prose union cannot swallow the symbol.
            right.append(span)
        else:
            left.append(span)
    groups = [group for group in (left, right) if group]
    if len(groups) <= 1 and groups:
        return [_line_from(line, groups[0])]
    return [_line_from(line, group) for group in groups]


def _covers_formula(lines, formulas):
    if not lines:
        return False
    box = _union([line['bbox'] for line in lines])
    return any(_y_overlap(box, formula) > 0.8 and _x_overlap(box, formula) > 0.8 for formula in formulas)


def _blocks_from_lines(template, lines, formulas):
    """Do not let a prose union rectangle swallow a symbol that sits between lines."""
    pieces = []
    for line in lines:
        pieces.extend(_split_around(line, formulas))
    if not pieces:
        return []
    groups = []
    for line in sorted(pieces, key=lambda item: (item['bbox'][1], item['bbox'][0])):
        placed = False
        for group in groups:
            if not _covers_formula(group + [line], formulas):
                group.append(line)
                placed = True
                break
        if not placed:
            groups.append([line])
    return [block for group in groups if (block := _copy_block(template, group))]


def _copy_block(template, lines):
    bbox = _union([line['bbox'] for line in lines])
    block = {key: value for key, value in template.items()
             if key not in {'lines', 'bbox', 'stacked_selectivity', 'stacked_markup', 'nonwhitespace_bbox'}}
    block['type'] = template.get('type', 0)
    block['bbox'] = bbox
    block['lines'] = lines
    return block


def _rebuild(block, absorbed, formulas):
    if block.get('type') == 1 or block.get('stacked_selectivity'):
        return [block]
    lines = []
    touched = False
    for line in block.get('lines') or []:
        kept = []
        for span in line.get('spans') or []:
            if id(span) in absorbed:
                touched = True
                continue
            if (span.get('text') or '').strip():
                kept.append(span)
        if kept:
            lines.append(_line_from(line, kept))
        elif line.get('spans'):
            touched = True
    if not touched:
        return [block]
    return _blocks_from_lines(block, lines, formulas)


def synthesize_stacked_selectivity(blocks):
    """Pull stacked S / ``+`` / ion glyphs into one non-prose block per symbol.

    Mutates ``blocks`` in place. Safe to call twice: a block already marked
    ``stacked_selectivity`` is left intact.
    """
    if not blocks:
        return blocks
    indexed = _iter_spans(blocks)
    clusters = _collect_clusters(indexed)
    if not clusters:
        return blocks
    absorbed = {id(span) for cluster in clusters for span in cluster}
    formulas = [_union([span['bbox'] for span in cluster]) for cluster in clusters]
    rebuilt = []
    for block in blocks:
        rebuilt.extend(_rebuild(block, absorbed, formulas))
    rebuilt.extend(_formula_block(cluster) for cluster in clusters)
    blocks[:] = rebuilt
    return blocks
