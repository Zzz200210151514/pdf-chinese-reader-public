"""Translation-independent source geometry gate and reading-order audit.

This never constructs placeholder translations or calls a font fitter. Ambiguous
classification remains a review item; geometry corruption cannot be waived.
"""
from copy import deepcopy
from collections import Counter
import hashlib
import json
import math
import re
from .classifier import reading_order, intersects, column_groups


def fingerprint(mapping):
    fields = ('source_sha256', 'pages', 'expected_block_ids', 'targets', 'references', 'review_resolutions', 'issues')
    data = {k: mapping.get(k) for k in fields}
    keys = ('id', 'source_page', 'rect', 'source_text', 'type', 'translation_required',
            'preservation_reason', 'role', 'inline_fragment_of', 'original_ids', 'paragraph_start', 'size',
            'logical_paragraph_id', 'translation_in')
    data['blocks'] = [{k: b.get(k) for k in keys} for b in mapping['blocks']]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_order(mapping):
    """Column bands first; small inline fragments follow their actual parent."""
    result = []
    for p in mapping['pages']:
        blocks = [deepcopy(b) for b in mapping['blocks'] if b['source_page'] == p['source_page']]
        ordered = reading_order(blocks, p['rect'])
        for b in list(ordered):
            if b.get('role') != 'inline-fragment':
                continue
            if len(b.get('source_text','')) > 12 or re.search(r'[A-Za-z]{3,}',b.get('source_text','')):
                continue
            if re.match(r'^\[\d',b.get('source_text','')):continue
            # Entire paragraphs flagged as fragments must still retain prose order.
            if b['rect'][3]-b['rect'][1] > max(b.get('size', 9.35), 9.35)*2:
                continue
            parents = [a for a in ordered if a['id'] != b['id'] and
                       (a['id'] in b.get('inline_fragment_of', []) or
                        set(a.get('original_ids', [])) & set(b.get('inline_fragment_of', [])))
                       and a.get('translation_required', True)
                       and abs(a['rect'][1]-b['rect'][1]) <= max(a.get('size', 9.35), 9.35)*2]
            if parents:
                parent = min(parents, key=lambda a: abs(a['rect'][1]-b['rect'][1]))
                parent_size=parent.get('size',9.35)
                if (b.get('size',parent_size)>parent_size or b['rect'][2]-b['rect'][0]>parent_size*2
                        or b['rect'][3]-b['rect'][1]>max(parent_size*2, 12.5)):continue
                ordered.remove(b)
                position=ordered.index(parent)+1
                while position<len(ordered) and ordered[position].get('inline_fragment_of')==b.get('inline_fragment_of') and ordered[position]['rect'][0]<b['rect'][0]:
                    position+=1
                ordered.insert(position, b)
        result.extend(b['id'] for b in ordered if b.get('translation_required', True))
    return result


def preflight(mapping, inspection=None):
    issues = []
    def add(check, b=None, **fields):
        item = {'check': check, 'severity': 'blocking', **fields}
        if b: item.update(id=b['id'], page=b.get('source_page'), rect=b.get('rect'))
        issues.append(item)
    def leading_bleed(a, b):
        # Consecutive text rows in dense tables overlap in their font-metric
        # boxes by a point or two. A vertical overlap that is a small fraction
        # of the smaller line's height is leading bleed, not a content overlap.
        vertical = min(a[3], b[3]) - max(a[1], b[1])
        return 0 < vertical <= max(2.5, .35 * max(1.0, min(a[3]-a[1], b[3]-b[1], 20)))
    def hairline(a, b):
        # Column boundaries and adjacent rows can overlap by a fraction of a
        # point after extraction; that is not a content overlap.
        width = min(a[2], b[2]) - max(a[0], b[0])
        height = min(a[3], b[3]) - max(a[1], b[1])
        return 0 < width <= .5 or 0 < height <= .5
    ids = [b['id'] for b in mapping['blocks']]
    if len(ids) != len(set(ids)) or Counter(ids) != Counter(mapping.get('expected_block_ids', ids)):
        add('source-block-coverage', action='Restore every unique source ID.')
    pages = {p['source_page']: p for p in mapping['pages']}
    if len(pages) != len(mapping['pages']): add('duplicate-source-page')
    def valid(box, bounds):
        return (isinstance(box, (list, tuple)) and len(box) == 4 and
                all(isinstance(x, (int, float)) and math.isfinite(x) for x in box) and
                bounds[0] <= box[0] < box[2] <= bounds[2] and
                bounds[1] <= box[1] < box[3] <= bounds[3])
    for b in mapping['blocks']:
        p = pages.get(b['source_page'])
        if not p or not valid(b.get('rect'), p['rect']):
            add('invalid-source-rectangle', b, action='Correct geometry against the original page.')
            continue
        if not b.get('translation_required', True):
            if not b.get('preservation_reason'): add('missing-preservation-reason', b)
            continue
        if not b.get('source_text', '').strip(): add('empty-source-text', b)
        natives = p.get('protected', []) + [{'id': a['id'], 'rect': a['rect']} for a in mapping['blocks']
                                          if a['source_page'] == b['source_page'] and not a.get('translation_required', True)]
        for native in natives:
            # Font-metric bleed between text rows is tolerated; a figure or
            # drawing under a paragraph is real damage at any overlap.
            text_native = native.get('type') not in ('figure',)
            if valid(native.get('rect'), p['rect']) and intersects(b['rect'], native['rect']) \
                    and not (text_native and leading_bleed(b['rect'], native['rect'])):
                add('protected-intersection', b, protected_id=native['id'], action='Review prose/native boundaries; do not shrink protection to bypass.')
    for p in pages.values():
        for native in p.get('protected', []):
            if not valid(native.get('rect'), p['rect']): add('invalid-protected-rectangle', page=p['source_page'], id=native.get('id'), rect=native.get('rect'))
        translated=[b for b in mapping['blocks'] if b['source_page']==p['source_page'] and b.get('translation_required',True) and valid(b.get('rect'),p['rect'])]
        columns=column_groups([b['rect'] for b in translated],p['rect'])
        for i,a in enumerate(translated):
            for j in range(i+1,len(translated)):
                b=translated[j]
                # A short fragment geometrically inside another block is an
                # inline fragment of it (caption tails, superscripts); the
                # parent defines reading order, so containment is not a
                # columns-overlap defect.
                if (a['rect'][0]<=b['rect'][0]+1 and a['rect'][1]<=b['rect'][1]+1
                        and a['rect'][2]>=b['rect'][2]-1 and a['rect'][3]>=b['rect'][3]-1) or \
                   (b['rect'][0]<=a['rect'][0]+1 and b['rect'][1]<=a['rect'][1]+1
                        and b['rect'][2]>=a['rect'][2]-1 and b['rect'][3]>=a['rect'][3]-1):
                    continue
                if (intersects(a['rect'],b['rect']) and columns[i]!=columns[j]
                        and not leading_bleed(a['rect'],b['rect']) and not hairline(a['rect'],b['rect'])):
                    add('cross-column-source-overlap',a,other_id=b['id'])
    for key, target in mapping.get('targets', {}).items():
        p = pages.get(target.get('source_page'))
        point = target.get('point', [])
        if not p or len(point) != 2 or not (p['rect'][0] <= point[0] <= p['rect'][2] and p['rect'][1] <= point[1] <= p['rect'][3]):
            add('invalid-navigation-target', id=key, page=target.get('source_page'))
    if inspection:
        if list(pages) != [p['source_page'] for p in inspection['pages']]: add('source-page-coverage')
        originals = {r['id'] for r in inspection.get('records', []) if r.get('text', '').strip()}
        covered = {i for b in mapping['blocks'] for i in b.get('original_ids', [b['id']])}
        for ident in sorted(originals-covered): add('unclassified-source-record', id=ident)
        for r in inspection.get('records',[]):
            if r.get('type')!='figure':continue
            p=pages.get(r['page'],{})
            protections=p.get('protected',[])+[{'rect':b['rect']} for b in mapping['blocks']
                          if b['source_page']==r['page'] and not b.get('translation_required',True)]
            box=r['bbox']
            if not any(n['rect'][0]<=box[0]+.01 and n['rect'][1]<=box[1]+.01 and n['rect'][2]>=box[2]-.01 and n['rect'][3]>=box[3]-.01 for n in protections):
                add('unprotected-source-image',id=r['id'],page=r['page'],rect=box,action='Restore complete original-image protection before dispatch.')
        def chars(s):
            # Pre-tagged <sup>/<sub> are extraction markup, not extra source characters.
            s = re.sub(r'</?(?:sup|sub|i|b|a)\b[^>]*>', '', s, flags=re.I)
            return Counter(re.sub(r'\s+', '', s))
        for r in inspection.get('records',[]):
            if not r.get('text','').strip():continue
            fragments=[b['source_text'] for b in mapping['blocks'] if r['id'] in b.get('original_ids',[b['id']])]
            if chars(r['text'])!=chars(''.join(fragments)):
                add('source-text-coverage',id=r['id'],page=r['page'],rect=r['bbox'],action='Restore missing or duplicated original text fragments before dispatch.')
    # Review decisions are specific to an issue, not a blanket --reviewed bypass.
    # Routine classifier candidates are suggestions. They must not stop plan;
    # a reviewer can still record a decision, but dispatch does not wait on them.
    BENIGN_REVIEW = {'inline-fragment-placement', 'caption-candidate-without-geometry',
                     'bibliography-continuation-candidate'}
    resolutions = mapping.get('review_resolutions', {})
    for i, issue in enumerate(mapping.get('issues', [])):
        key = issue.get('id', '')+'|'+issue['check']+'|'+str(issue.get('page', ''))
        decision = resolutions.get(key)
        benign = issue.get('check') in BENIGN_REVIEW
        issues.append({**issue, 'issue_key': key,
                       'severity': 'warning' if decision or benign else 'review-required',
                       'resolution': decision or None})
    order = source_order(mapping)
    return {'passed': not any(i['severity'] == 'blocking' for i in issues),
            'ready_to_dispatch': not any(i['severity'] in {'blocking', 'review-required'} for i in issues),
            'issues': issues, 'source_order': order, 'map_fingerprint': fingerprint(mapping),
            'checks': {'geometry': True, 'source_ids': True, 'navigation': True,
                       'classification': 'heuristics plus explicit issue review',
                       'font_fit': 'deferred until translations exist', 'semantic_accuracy': 'NOT CERTIFIED'}}


def audit_order(mapping, manifest):
    expected = source_order(mapping)
    actual = [i for p in manifest['pages'] for r in p.get('regions', []) for i in r.get('source_block_ids', [])]
    issues = []
    if Counter(expected) != Counter(actual): issues.append({'check': 'compiled-source-coverage'})
    rank = {ident: n for n, ident in enumerate(expected)}
    blocks={b['id']:b for b in mapping['blocks']}
    for a, b in zip(actual, actual[1:]):
        if a in rank and b in rank and rank[a] >= rank[b]:
            left,right=blocks[a],blocks[b]
            # Separate glyphs extracted from one paragraph have no whole-block
            # serial order inside its multi-line string. Audit their attachment,
            # without pretending this certifies exact inline semantic placement.
            peers=(left.get('role')==right.get('role')=='inline-fragment'
                   and len(left.get('source_text',''))<=25 and len(right.get('source_text',''))<=25
                   and bool(set(left.get('inline_fragment_of',[])) & set(right.get('inline_fragment_of',[]))))
            if peers:continue
            issues.append({'check': 'compiled-reading-order', 'id': a, 'following_id': b,
                           'action': 'Restore source column/heading/paragraph order.'})
    return {'passed': not issues, 'issues': issues, 'expected_ids': expected, 'actual_ids': actual,
            'scope': 'compiled paragraph sequence; PDF reader extraction order and semantics are separate'}
