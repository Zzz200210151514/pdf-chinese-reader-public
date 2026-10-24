"""Source-evidenced citation typography and link colours, without guessing numbers."""
from collections import Counter
import re

GROUP = re.compile(r'\d+(?:\s*[,，–−-]\s*\d+)*')
# A raised digit after a unit is an exponent (`cm2`, `m2`), not a citation.
UNIT_TAIL = re.compile(
    r'(?:cm|mm|nm|μm|µm|dm|km|m|kg|mg|g|mol|mmol|μL|µL|mL|L|mA|A|mV|V|kW|W|'
    r'kHz|Hz|kPa|MPa|Pa|min|h|s|K|ppm|wt|eV)\s*$', re.I)


def canonical(text):
    return re.sub(r'\s+', '', text).replace('，', ',').replace('−', '-').replace('–', '-')


def _colour(span):
    value = span.get('color')
    return f'#{value:06x}' if isinstance(value, int) and 0 <= value <= 0xffffff else None


def source_citations(lines, known):
    groups = []
    for line in lines or []:
        spans = line.get('spans', [])
        pending = []
        def flush():
            if pending:
                text = ''.join(s.get('text', '') for s in pending).strip()
                if GROUP.fullmatch(text) and all('ref/'+n in known for n in re.findall(r'\d+', text)):
                    groups.append((text, _colour(pending[0])))
                pending.clear()
        for index, span in enumerate(spans):
            text = span.get('text', '').strip()
            base = next((s for s in reversed(spans[:index]) if s.get('size', 0) > span.get('size', 0)*1.15), None)
            raised = bool(span.get('flags', 0) & 1)
            if base and 'origin' in span and 'origin' in base:
                raised |= span['origin'][1] < base['origin'][1] - max(.4, base['size']*.08)
            # Numeric powers after variables / units are not bibliography citations.
            prose = base and (len(base.get('text', '').strip()) > 3 or re.search(r'[.;。；]$', base.get('text', '').strip()))
            # `cm2` after a unit is an area exponent even when the base run is a
            # long prose span; require the base to actually end in prose, not a
            # unit symbol.
            if prose and UNIT_TAIL.search(base.get('text', '')):
                prose = False
            coloured = span.get('color', 0) not in (0, 0xffffff)
            if raised and (prose or coloured or pending) and re.fullmatch(r'[\d\s,，–−-]+', text):
                pending.append(span)
            else:
                flush()
        flush()
    return groups


def restore_citations(text, lines, known):
    """Mark only uniquely located native superscript groups; report ambiguity."""
    groups = source_citations(lines, known)
    expected = Counter(canonical(g) for g, _ in groups)
    parts = re.split(r'(<[^>]+>|\[\[ref:\d+(?:\|[^\]]+)?\]\])', text)
    found = Counter()
    matches = []
    depth = 0
    for i, part in enumerate(parts):
        if i % 2:
            marker = re.fullmatch(r'\[\[ref:(\d+)(?:\|[^\]]+)?\]\]', part)
            if marker:
                found[marker[1]] += 1
                continue
            if re.match(r'<(?:sup|sub)\b', part): depth += 1
            elif re.match(r'</(?:sup|sub)>', part): depth = max(0, depth-1)
            continue
        for m in GROUP.finditer(part):
            key = canonical(m[0])
            if key not in expected: continue
            # Do not select a substring of a decimal, chemical or identifier.
            before = part[m.start()-1:m.start()] if m.start() else ''
            after = part[m.end():m.end()+1]
            # ACS-style sentence-final citations sit directly after the period
            # (`migration.27`). The group being a known native superscript is
            # decisive evidence: allow the period boundary, reject digits that
            # would continue a decimal (`3.27` never matches here since GROUP
            # consumes the leading integer).
            sentence_final = before == '.' and key in expected and not re.fullmatch(r'[0-9]', after or ' ')
            if not sentence_final and (re.fullmatch(r'[A-Za-z0-9.+−-]', before or ' ') or re.fullmatch(r'[A-Za-z0-9.+−-]', after or ' ')): continue
            if depth:
                if '<sup>' in ''.join(parts[:i]): found[key] += 1
            else:
                matches.append((i, m.start(), m.end(), key))
    counts = Counter(k for _, _, _, k in matches)
    issues = []
    for key, count in expected.items():
        if found[key] == count: continue
        if found[key]+counts[key] != count:
            issues.append({'check':'citation-alignment-needs-review','citation':key,
                           'action':'Numerals duplicate a data value; wrap the citation occurrence in <sup>...</sup>.'})
    bad = {i['citation'] for i in issues}
    for i, start, end, key in reversed(matches):
        if key not in bad and found[key] < expected[key]:
            parts[i] = parts[i][:start]+'<sup>'+parts[i][start:end]+'</sup>'+parts[i][end:]
    colours = {}
    for group, colour in groups:
        usable = usable_link_hex(colour)
        if not usable:
            continue
        for n in re.findall(r'\d+', group):
            colours['ref/'+n] = usable
    return ''.join(parts), {canonical(g) for g, _ in groups}, colours, issues


def _joined_spans(lines):
    """Concatenate adjacent lines so a wrapped ``Figure`` / ``2a`` pair stays one ref."""
    parts, offsets = [], []
    for line in lines or []:
        for span in line.get('spans') or []:
            text = span.get('text') or ''
            if not isinstance(text, str) or not text:
                continue
            parts.append(text)
            offsets.extend([span] * len(text))
    return ''.join(parts), offsets


def _object_kind(word):
    folded = word.lower()
    if folded.startswith('fig'):
        return 'fig'
    if folded.startswith('table'):
        return 'table'
    return 'eq'


def _link_hex(span):
    """Journal link blue as ``#rrggbb``, or None for body ink and other colours."""
    if not isinstance(span, dict) or not _is_link_blue(span.get('color')):
        return None
    return _colour(span)


def usable_link_hex(value):
    """Normalise a stored colour when it is journal link blue."""
    if isinstance(value, int):
        return _link_hex({'color': value})
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', value):
        return None
    return _link_hex({'color': int(value[1:], 16)})


def object_colours(lines, known):
    """Map fig/table/eq/ref keys to source link blue.

    Adjacent lines are joined first, so a digit wrapped onto the next line
    (``...(Figure`` / ``2a).``) contributes its own ink. Black caption titles
    and other non-link colours are ignored and must not enter the map.
    """
    colours = {}
    text, offsets = _joined_spans(lines)
    if not text:
        return colours

    def paint(key, index):
        if key not in known or index >= len(offsets):
            return
        colour = _link_hex(offsets[index])
        if colour:
            colours[key] = colour

    for match in re.finditer(r'\b(Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?)\s*\(?(\d+)', text, re.I):
        paint(_object_kind(match[1])+'/'+match[2], match.start(2))
    for group in re.finditer(r'\[(\d+(?:\s*[,–−-]\s*\d+)*)\]', text):
        for number in re.finditer(r'\d+', group[1]):
            paint('ref/'+number[0], group.start(1)+number.start())
    return colours


_CAPTION_TITLE = re.compile(
    r'\s*(Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?)\s*\(?(\d+)', re.I)


def caption_title_ink(lines):
    """Body ink of a leading figure/table/equation label.

    Returns ``(key, '#rrggbb')`` when the label is present and is not journal
    link blue, so caption titles keep source body ink. Link-blue titles and
    lines that do not open with a label return ``(None, None)``.
    """
    text, offsets = _joined_spans(lines)
    match = _CAPTION_TITLE.match(text)
    if not match or match.start(2) >= len(offsets):
        return None, None
    span = offsets[match.start(2)]
    if _is_link_blue(span.get('color')):
        return None, None
    return _object_kind(match[1])+'/'+match[2], _colour(span) or '#000000'


def dominant_link_blue(lines):
    """Most common journal hyperlink blue across source lines, or None.

    Each link-blue span counts once. Body ink, including black caption titles,
    does not compete. Ties keep the colour encountered first.
    """
    counts = Counter()
    for line in lines or []:
        for span in line.get('spans') or []:
            colour = _link_hex(span)
            if colour:
                counts[colour] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


# Author-year cite painted in journal link blue, e.g. "Kim et al., 2016".
AUTHOR_YEAR = re.compile(
    r"(?:[A-Z][A-Za-z'\-]+(?:\s+(?:and|&)\s+[A-Z][A-Za-z'\-]+)?(?:\s+et\s+al\.?)?,\s*(?:19|20)\d{2}[a-z]?)"
    r"|(?:[A-Z][A-Za-z'\-]+\s+et\s+al\.?)"
    r"|(?:[A-Z][A-Za-z'\-]+,\s*(?:19|20)\d{2}[a-z]?)"
)


def _is_link_blue(color):
    """True for journal hyperlink blue, not near-black body ink."""
    if not isinstance(color, int) or not 0 <= color <= 0xffffff:
        return False
    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    return b >= 100 and b > r + 30 and b > g + 30


def author_year_token(text):
    """Return a cleaned author-year token, or None if the span is not one."""
    value = re.sub(r'[\U00010000-\U0010ffff]', '', text or '')
    value = value.strip().strip('()[]（）.,;；')
    value = re.sub(r'\s+', ' ', value).strip()
    if len(value) < 6:
        return None
    # Bare years are too ambiguous for colouring.
    if re.fullmatch(r'(?:19|20)\d{2}[a-z]?', value):
        return None
    if AUTHOR_YEAR.fullmatch(value) or (
            re.search(r'(?:19|20)\d{2}[a-z]?$', value)
            and re.search(r'[A-Za-z]', value)
            and (',' in value or re.search(r'\bet\s+al\b', value, re.I))):
        return value
    return None


def author_year_colours(lines):
    """Map source-evidenced author-year cite strings to their link colour."""
    colours = {}
    for line in lines or []:
        for span in line.get('spans', []):
            if not _is_link_blue(span.get('color')):
                continue
            token = author_year_token(span.get('text', ''))
            if token:
                colours[token] = _colour(span)
    return colours


def is_author_year_cite(text):
    return author_year_token(text) is not None
