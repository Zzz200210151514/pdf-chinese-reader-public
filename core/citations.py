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
    colours = {'ref/'+n: colour for group, colour in groups if colour for n in re.findall(r'\d+', group)}
    return ''.join(parts), {canonical(g) for g, _ in groups}, colours, issues


def object_colours(lines, known):
    colours = {}
    for line in lines or []:
        spans = line.get('spans', [])
        text = ''.join(s.get('text', '') for s in spans)
        offsets = []
        for span in spans:
            offsets.extend([span] * len(span.get('text', '')))
        for m in re.finditer(r'\b(Fig(?:ure)?\.?|Table|Eq(?:uation)?\.?)\s*\(?(\d+)', text, re.I):
            kind = 'fig' if m[1].lower().startswith('fig') else 'table' if m[1].lower().startswith('table') else 'eq'
            colour = _colour(offsets[m.start(2)])
            if colour and kind+'/'+m[2] in known: colours[kind+'/'+m[2]] = colour
        for group in re.finditer(r'\[(\d+(?:\s*[,–−-]\s*\d+)*)\]', text):
            for number in re.finditer(r'\d+', group[1]):
                colour = _colour(offsets[group.start(1)+number.start()])
                if colour and 'ref/'+number[0] in known: colours['ref/'+number[0]] = colour
    return colours
