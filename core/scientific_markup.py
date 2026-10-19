"""Conservative rendering-only restoration of native scientific typography."""
from __future__ import annotations

import re

_ELEMENTS = set("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split())
_UNITS = set("m mm cm km nm pm s ms min h g kg mg mol mmol K Pa kPa MPa J kJ W kW A V Hz L mL dL R mR Gy mGy Sv mSv".split())
_TAIL = re.compile(r"(?<![A-Za-zα-ωΑ-Ω])([A-Za-zα-ωΑ-Ω]+)$")
_FRAGMENT = re.compile(r"[0-9+−-]+|[A-Za-zα-ωΑ-Ω]")
_PARTS = re.compile(r"(<[^>]+>|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\])")


def _base_kind(span):
    match = _TAIL.search(span.get("text", ""))
    if not match:
        return None, None
    token = match[1]
    if len(token)==1 and (re.fullmatch(r'[α-ωΑ-Ω]',token) or span.get('flags',0)&2):
        return token,'variable'
    if token in _UNITS:
        return token, "unit"
    elements = re.findall(r"[A-Z][a-z]?", token)
    if elements and "".join(elements) == token and all(e in _ELEMENTS for e in elements):
        return token, "chemical"
    # Arbitrary prose tails are unsafe; mixed-case short variables or native
    # italic single letters provide stronger evidence of mathematical notation.
    if re.fullmatch(r"[a-z][A-Z]", token) or (len(token) == 1 and span.get("flags", 0) & 2):
        return token, "variable"
    return None, None


def _position(base, small):
    try:
        size, base_size = float(small.get("size", 0)), float(base.get("size", 0))
        delta = float(small["origin"][1]) - float(base["origin"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not 0 < size < base_size * .85 or abs(delta) < max(.4, base_size * .08):
        return None
    return "sup" if delta < 0 else "sub"


def _replacements(lines):
    candidates = {}
    for line in lines or []:
        if any(line.get(key) in ("formula", "equation", "math") for key in ("type", "kind", "category")):
            continue
        spans = line.get("spans", [])
        if len(spans)>1 and re.fullmatch(r'\d+[+−-]',spans[0].get('text','')):
            # A leading charge cut from the preceding block stays at this exact
            # position; the normal-sized same-line neighbor provides baseline.
            charge=spans[0]
            neighbor=next((s for s in spans[1:] if s.get('size',0)>charge.get('size',0)*1.2),None)
            if neighbor and _position(neighbor,charge)=='sup':
                token=charge['text'];candidates.setdefault(token,set()).add('<sup>'+token+'</sup>')
        for index, base in enumerate(spans):
            token, kind = _base_kind(base)
            if token is None:
                continue
            fragments = []
            for small in spans[index + 1:]:
                position = _position(base, small)
                fragment = small.get("text", "")
                if position is None or not _FRAGMENT.fullmatch(fragment):
                    break
                fragments.append((position, fragment))
            if not fragments:
                continue
            # A chemical superscript integer alone could be a citation. Charge
            # signs must occur in the same native superscript run to justify it.
            runs = []
            for position, fragment in fragments:
                if runs and runs[-1][0] == position:
                    runs[-1] = (position, runs[-1][1] + fragment)
                else:
                    runs.append((position, fragment))
            if any(not re.fullmatch(r"[0-9A-Za-zα-ωΑ-Ω]+" if kind=='variable' else r"\d+", fragment) if position == "sub"
                   else not re.fullmatch(r"(?:\d+[+−-]|[+−-]\d*|\d+)", fragment)
                   for position, fragment in runs):
                continue
            if kind == "chemical" and any(position == "sup" and not re.search(r"[+−-]", fragment)
                                           for position, fragment in runs):
                continue
            original = token + "".join(fragment for _, fragment in runs)
            marked = token + "".join(f"<{position}>{fragment}</{position}>" for position, fragment in runs)
            candidates.setdefault(original, set()).add(marked)
    # Conflicting native baselines cannot justify a global text replacement.
    return {token: next(iter(values)) for token, values in candidates.items() if len(values) == 1}


def restore_scientific_markup(translation, lines):
    """Add sub/sup only to exact tokens evidenced by smaller, shifted spans.

    No source data is mutated and removing inserted tags leaves every character
    unchanged. Native spans must be on the same line; isolated charges are never
    moved across lines. Existing tags and explicit LaTeX formulas are preserved.
    shortcut: a plain repeated token inherits consistent source formatting;
    use occurrence alignment if sources contain both styled and unstyled copies.
    """
    replacements = _replacements(lines)
    if not replacements:
        return translation
    pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(token) for token in sorted(replacements, key=len, reverse=True)) + r")(?![A-Za-z0-9+−-])")
    parts = _PARTS.split(translation)
    depth = 0
    for index, part in enumerate(parts):
        if index % 2:
            if re.match(r"<(?:sub|sup)\b", part, re.I):
                depth += 1
            elif re.match(r"</(?:sub|sup)\s*>", part, re.I):
                depth = max(0, depth - 1)
        elif not depth:
            parts[index] = pattern.sub(lambda match: replacements[match[0]], part)
    return "".join(parts)
