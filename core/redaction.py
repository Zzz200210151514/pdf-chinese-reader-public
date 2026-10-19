"""Keep native character bounds separate from visible/layout rectangles."""
from collections import Counter
import pymupdf as fitz


def split_masks(masks, guards):
    pieces = [fitz.Rect(r) for r in masks]
    for guard in guards:
        g = fitz.Rect(guard['rect'])
        if g.is_infinite or g.x0 > g.x1 or g.y0 > g.y1:
            raise ValueError('Invalid native character guard')
        # Zero-advance combining accents have no intersectable font rectangle.
        # Their existing native visual protection and final pixel check remain.
        if g.is_empty:
            continue
        updated = []
        for r in pieces:
            cut = r & g
            if cut.is_empty:
                updated.append(r)
                continue
            candidates = [fitz.Rect(r.x0, r.y0, r.x1, cut.y0),
                          fitz.Rect(r.x0, cut.y1, r.x1, r.y1),
                          fitz.Rect(r.x0, cut.y0, cut.x0, cut.y1),
                          fitz.Rect(cut.x1, cut.y0, r.x1, cut.y1)]
            updated.extend(p for p in candidates if not p.is_empty)
        pieces = updated
    return [list(r) for r in pieces]


def _key(font, codepoint, origin):
    return font, codepoint, round(origin[0], 3), round(origin[1], 3)


def _remaining(page, masks):
    # Trial documents are isolated; never mutate the source or embed Chinese.
    with fitz.open() as trial:
        trial.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        copy = trial[0]
        for r in masks:
            copy.add_redact_annot(r, fill=False, cross_out=False)
        if masks:
            copy.apply_redactions(images=0, graphics=0, text=0)
            copy = trial.reload_page(copy)
        return Counter(_key(t['font'], c[0], c[2])
                       for t in copy.get_texttrace() for c in t['chars']
                       if chr(c[0]).strip())


def _refine(page, spec, baseline, candidate, allowed):
    """Try bounded character-local masks; accept no collateral glyph removal."""
    extra=(candidate-baseline)-allowed
    if sum(extra.values()) > 16:
        return candidate
    char_boxes={_key(t['font'],c[0],c[2]):fitz.Rect(c[3])
                for t in page.get_texttrace() for c in t['chars']}
    trials=0;recovered=0
    for key in list(extra):
        if key not in extra or key not in char_boxes:continue
        box=char_boxes[key]
        for fx,fy in [(x,y) for y in (.9,.5,.1) for x in (.5,.1,.9)]:
            x=box.x0+box.width*fx;y=box.y0+box.height*fy
            mask=fitz.Rect(x-.025,y-.025,x+.025,y+.025)
            if not any(fitz.Rect(r).contains(mask) for r in spec['redactions_original']):continue
            trials+=1
            result=_remaining(page,spec['redactions']+[list(mask)])
            removed=candidate-result
            if removed and not (removed-extra) and not (result-candidate):
                spec['redactions'].append(list(mask));candidate=result
                recovered+=sum(removed.values());extra=(candidate-baseline)-allowed
                if key not in extra:break
    if recovered:spec.setdefault('erasure_repairs',[]).append({'removed_source_characters':recovered,'local_trials':trials})
    return candidate


def check_erasure_coverage(page, spec, repair=False):
    """A split may retain native glyphs, but must not leave additional prose.

    Run only for changed mask sets. Compare actual MuPDF results, because font
    metrics and ink bounds do not fully predict its erasure behavior.
    """
    original = spec.get('redactions_original', spec.get('redactions', []))
    safe = spec.get('redactions', [])
    if original == safe:
        return []
    relevant = [g for g in spec.get('erase_guards', [])
                if any(fitz.Rect(g['rect']).intersects(fitz.Rect(r)) for r in original)]
    spans = [s for b in page.get_text('rawdict')['blocks'] if b['type'] == 0
             for line in b['lines'] for s in line['spans']]
    allowed = Counter()
    seen = set()
    for guard in relevant:
        identity = (guard['font'], tuple(guard['origin']), guard['text'])
        if identity in seen:
            continue
        seen.add(identity)
        matches = [s for s in spans if s['font'] == guard['font']
                   and max(abs(s['origin'][i]-guard['origin'][i]) for i in (0, 1)) < .01
                   and ''.join(c['c'] for c in s['chars']).strip() == guard['text'].strip()]
        if len(matches) != 1:
            return [{'check': 'erase-guard-source-mismatch', 'page': spec['source_page'],
                     'id': guard.get('id'), 'action': 'Review the native span identity; do not bypass.'}]
        allowed.update(_key(guard['font'], ord(c['c']), c['origin'])
                       for c in matches[0]['chars'] if c['c'].strip())
    baseline = _remaining(page, original)
    candidate = _remaining(page, safe)
    if repair and (candidate-baseline)-allowed:
        candidate = _refine(page,spec,baseline,candidate,allowed)
    extra = (candidate-baseline)-allowed
    lost = baseline-candidate
    if extra or lost:
        return [{'check': 'erase-coverage-conflict', 'page': spec['source_page'],
                 'extra_source_characters': sum(extra.values()),
                 'unexpected_removed_characters': sum(lost.values()),
                 'action': 'Native bounds conflict with prose erasure; review this page without changing the translation.'}]
    return []
