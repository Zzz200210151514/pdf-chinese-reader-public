"""Deterministic protection of chemistry, numeric values, units and citations."""
from collections import Counter
import hashlib
import re
import unicodedata

ELEMENTS = 'H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr'.split()
ELEMENT = '(?:' + '|'.join(sorted(ELEMENTS, key=len, reverse=True)) + ')'
CHEM = re.compile(r'(?<![A-Za-z])(?:[¹²³⁴⁵⁶⁷⁸⁹⁰\d]+)?(?:' + ELEMENT + r'[₀-₉\d]*|\((?:' + ELEMENT + r'[₀-₉\d]*)+\)[₀-₉\d]*)+(?:[⁺⁻+−-]|[²³⁴⁵⁶⁷⁸⁹\d]+[⁺⁻+−-])?(?![A-Za-z])')
# Element-symbol concatenation false-positives. These stay unfrozen unless the
# match also carries a digit, charge or subscript (H2O, Fe3+, SO4).
ROMAN_NUMERALS = {'II', 'III', 'IV', 'VI', 'VII', 'VIII', 'IX', 'XI', 'XII'}
ENGLISH_CAPS = {'OF', 'IN', 'ON', 'BY', 'NO', 'SO', 'US', 'UP', 'OK', 'OR', 'AS', 'AT',
                'TO', 'IF', 'IS', 'IT', 'BE', 'HE', 'WE', 'AM', 'PM'}
SCIENTIFIC_ACRONYMS = {'NF', 'UF', 'FO', 'PRO', 'BPM', 'CV', 'UV', 'PV', 'ICP', 'VOC',
                       'PAN', 'PS', 'PI', 'PP', 'PC', 'PU', 'RO', 'MD', 'MBR', 'ED',
                       'AEM', 'CEM', 'BPM', 'TOC', 'COD', 'BOD', 'SEM', 'TEM', 'XRD',
                       'XPS', 'FTIR', 'NMR', 'HPLC', 'GC', 'MS', 'AFM', 'EDS', 'EDX'}
CHEM_FALSE_POSITIVES = ROMAN_NUMERALS | ENGLISH_CAPS | SCIENTIFIC_ACRONYMS
# A leading-dot decimal (`.27`) never follows a word character or closing
# bracket in scientific prose: `migration.27` is sentence-final punctuation
# plus a superscript citation, not the decimal 0.27. Freeze the number alone
# and leave the period outside so citation restoration can see the boundary.
NUM = re.compile(r'(?<![0-9])[+-]?(?:\d+(?:[.,]\d+)*|(?<![\w)])\.\d+)(?:[eE][+-]?\d+)?(?:\s*[×x]\s*10(?:\^?[−-]?\d+|[⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+))?%?|[⁰¹²³⁴⁵⁶⁷⁸⁹]+')
UNIT = re.compile(r'(?<![A-Za-z])(?:mol|mmol|μmol|µmol|nmol|mg|μg|µg|kg|g|mL|ml|μL|µL|L|nm|μm|µm|mm|cm|m|mV|V|mA|A|Pa|kPa|MPa|Hz|kHz|MHz|s|min|h|K|°C|℃|eV|W)(?:[−-][1-9]|[⁻¹²³⁴⁵⁶⁷⁸⁹]+|\^[−-]?\d+)?(?![A-Za-z])')
TOKEN = re.compile(r'⟦V4_[a-f0-9]{8}_\d{4}⟧')


def protected_spans(source):
    spans = []
    for m in CHEM.finditer(source):
        value = m.group()
        # Single ordinary words matching an element (In, As) are not formulas.
        # Parentheses alone are not chemistry evidence either: English
        # `vanadium(V)` hides the `(V)` behind a Latin lookbehind while the
        # Chinese `钒(V)` does not, so a lone parenthesized element symbol must
        # audit the same on both sides. Digits, sub/superscripts, charges or a
        # second element still mark real formulas.
        # Roman numerals, all-caps function words and undressed membrane
        # acronyms share the element-symbol alphabet but are not formulas.
        if value in CHEM_FALSE_POSITIVES and not re.search(r'[\d₀-₉¹²³⁴⁵⁶⁷⁸⁹⁰⁺⁻+−]', value):
            continue
        if re.search(r'[\d₀-₉¹²³⁴⁵⁶⁷⁸⁹⁰⁺⁻+−]', value) or len(re.findall(ELEMENT, value)) > 1:
            spans.append((m.start(), m.end()))
    spans += [(m.start(), m.end()) for m in NUM.finditer(source)]
    for m in UNIT.finditer(source):
        # A at a sentence start is an article; m/s/h can be prose variables.
        # Freeze ambiguous one-letter units only in numerical/unit context.
        if m.group() in ('A', 'V', 'K', 'W', 'g', 'm', 'L', 's', 'h'):
            before = source[max(0, m.start()-24):m.start()]
            if not re.search(r'(?:[\d⁰¹²³⁴⁵⁶⁷⁸⁹]|/|\b(?:mg|mol|mmol|g|kg|mL|L|cm|m|A|V))\s*$', before):
                continue
        spans.append((m.start(), m.end()))
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def freeze(source):
    if TOKEN.search(source):
        raise ValueError('Source contains reserved V4 placeholder')
    prefix = hashlib.sha256(source.encode()).hexdigest()[:8]
    parts, frozen, offset = [], {}, 0
    for index, (start, end) in enumerate(protected_spans(source)):
        token = f'⟦V4_{prefix}_{index:04d}⟧'
        frozen[token] = source[start:end]
        parts.extend([source[offset:start], token])
        offset = end
    parts.append(source[offset:])
    return ''.join(parts), frozen


def restore(translation, frozen):
    """Put protected text back. A token may repeat; it may not be invented or dropped.

    Chinese clause order sometimes needs the same frozen quantity twice. That is
    valid. A placeholder the source never froze, or a source token that does not
    appear at least once while other placeholders remain, is still an error.

    Text with no placeholders is left unchanged. Block-cache entries and supplied
    revisions are already restored; a model reply that drops every placeholder is
    rejected by the router before this function runs, then audited for content.
    """
    counts = Counter(TOKEN.findall(translation))
    if not counts:
        return translation
    missing = [token for token in frozen if counts[token] < 1]
    unknown = [token for token in counts if token not in frozen]
    if missing or unknown:
        raise ValueError('Missing or unknown frozen placeholder')
    for token, original in frozen.items():
        translation = translation.replace(token, original)
    return translation


def canonical(value):
    return unicodedata.normalize('NFKC', value).replace('−', '-').replace('µ', 'μ').replace('℃', '°C')


def _audit_key(value):
    """Canonical form plus outer-parenthesis folding.

    English glues `(` to the preceding Latin letter (`molybdenum(VI)`), so the
    protected match is `VI`; a Chinese translation has a free-standing `(VI)`
    and matches the parenthesized form. Fold the outer parentheses on both
    sides so the audit compares meaning, not typography.
    """
    # Line breaks inside a protected span are extraction formatting, not
    # content; compare with whitespace removed on both sides.
    value = re.sub(r'\s+', '', canonical(value))
    if value.startswith('(') and value.endswith(')'):
        value = value[1:-1]
    return value


def _extend_charge(text, spans):
    """A raised charge belongs to the formula even when a word follows it."""
    result = []
    for start, end in spans:
        cursor = end
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor < len(text) and text[cursor] in '⁺⁻+−':
            end = cursor + 1
        result.append((start, end))
    return result


_NUM_TOKEN = re.compile(
    r'^[+-]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:[eE][+-]?\d+)?(?:[×x]10(?:\^?[−-]?\d+)?)?%?$'
    r'|^[⁰¹²³⁴⁵⁶⁷⁸⁹]+$')
_UNIT_TOKEN = re.compile(
    r'^(?:mol|mmol|μmol|µmol|nmol|mg|μg|µg|kg|g|mL|ml|μL|µL|L|nm|μm|µm|mm|cm|m|'
    r'mV|V|mA|A|Pa|kPa|MPa|Hz|kHz|MHz|s|min|h|K|°C|eV|W)'
    r'(?:[−-][1-9]|[⁻¹²³⁴⁵⁶⁷⁸⁹]+|\^[−-]?\d+)?$')


def _numeric_or_unit(token):
    """True when an audited token is a number or unit, not a chemical formula."""
    if _NUM_TOKEN.fullmatch(token) or _UNIT_TOKEN.fullmatch(token):
        return True
    # Adjacent number+unit spans are audited as one key after whitespace removal.
    return bool(re.fullmatch(
        r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:mg|g|mL|ml|L|mM|M|%|nm|μm|µm|mm|cm|m|s|h|min|K|°C|eV|mV|V|Pa|kPa|MPa|Hz|mol|mmol)',
        token))


def audit(source, translation):
    """Return protected-token differences, accepting Unicode semantic equivalents."""
    # Letter-spaced display headings (`H I G H T S`, `G R A P H I C A L`) make
    # single capital letters look like units (L = litre, A = ampere) and freeze
    # them. They carry no translatable protected content; any translation will
    # legitimately drop the isolated letters.
    if re.search(r'(?:\b[A-Za-z]\s+){4,}', source):
        return {'missing': [], 'added': []}
    # PDF extraction separates a chemical formula and its raised charge. Audit
    # their joined meaning on BOTH sides; never remove or ignore the charge.
    charge_space = re.compile(r'(?<![A-Za-z])((?:'+ELEMENT+r'[₀-₉\d]*){2,})\s+([⁺⁻+−-])(?!\s*\d|[A-Za-z])')
    source = charge_space.sub(r'\1\2', source)
    translation = charge_space.sub(r'\1\2', translation)
    expected = Counter(_audit_key(source[a:b]) for a, b in _extend_charge(source, protected_spans(source)))
    actual = Counter(_audit_key(translation[a:b]) for a, b in _extend_charge(translation, protected_spans(translation)))
    result = {'missing': list((expected - actual).elements()), 'added': list((actual - expected).elements())}
    # "tripled" → "3倍" and "Fig. 1a and b" → "图 1a 和图 1b" change numeric
    # counts without inventing chemistry. Callers record that as a warning.
    if result['missing'] or result['added']:
        if all(_numeric_or_unit(token) for token in result['missing'] + result['added']):
            result['severity'] = 'warning'
    # Two adjacent frozen tokens joined without their original punctuation
    # re-scan as one larger token, e.g. `(SPI)` + `21` -> `(SPI)21`. The audit
    # failure is real, but the message must say what to do about it.
    if result['missing'] and result['added']:
        for missing in result['missing']:
            for added in result['added']:
                if missing != added and len(missing) < len(added) and missing in added:
                    result['hint'] = ('相邻保护内容被无分隔拼接（原文以标点隔开的两个保护项在译文中被合并，'
                                      '如 (SPI),21 写成 (SPI)21）。请保留占位符之间的原文标点。')
    return result
