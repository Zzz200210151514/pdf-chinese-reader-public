"""Translate MinerU content-list blocks and build a paginated Chinese reading PDF.

MinerU supplies reading order and semantic regions; the source PDF supplies native
text checks and original media crops. No translation or external upload in finish.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

import pymupdf
import reflow_links


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'sub', 'sup', 'b', 'strong', 'i', 'em', 'br'}:
            self.parts.append('<' + tag + '>')

    def handle_endtag(self, tag):
        if tag in {'sub', 'sup', 'b', 'strong', 'i', 'em'}:
            self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        self.parts.append(html.escape(data))


def safe_markup(text):
    if not isinstance(text, str):
        raise ValueError('Text must be a string')
    parser = Markup()
    parser.feed(text)
    parser.close()
    return ''.join(parser.parts).replace('\n', ' ')


def plain(text):
    # Literal inequalities are scientific text, not tags spanning until the next >.
    return html.unescape(reflow_links.TAGS.sub('', text))


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', plain(text))).replace('\u00ad', '')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def source_region(item, doc):
    index, bbox = item.get('page_idx'), item.get('bbox')
    if type(index) is not int or not 0 <= index < len(doc):
        raise ValueError('Invalid MinerU page_idx')
    if (not isinstance(bbox, list) or len(bbox) != 4
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in bbox)
            or not (0 <= bbox[0] < bbox[2] <= 1000 and 0 <= bbox[1] < bbox[3] <= 1000)):
        raise ValueError('Invalid normalized MinerU bbox')
    page = doc[index]
    if page.rotation:
        raise ValueError('Rotated source pages need a reviewed coordinate adapter')
    rect = pymupdf.Rect(bbox[0] * page.rect.width / 1000, bbox[1] * page.rect.height / 1000,
                        bbox[2] * page.rect.width / 1000, bbox[3] * page.rect.height / 1000)
    return page, rect


def native_region(raw, rect):
    """Select glyph centres so touching neighbour paragraphs cannot bleed in."""
    lines = []
    for block in raw['blocks']:
        if block.get('type') != 0:
            continue
        for line in block.get('lines', []):
            chars = []
            for span in line['spans']:
                for char in span['chars']:
                    x0, y0, x1, y1 = char['bbox']
                    if rect.x0 <= (x0 + x1) / 2 <= rect.x1 and rect.y0 <= (y0 + y1) / 2 <= rect.y1:
                        chars.append(char['c'])
            if ''.join(chars).strip():
                lines.append(''.join(chars).strip())
    return '\n'.join(lines)


def normalize_items(items):
    """Keep service JSON immutable; adapt chart groups and references in memory."""
    result=[];pending=[]
    for index,original in enumerate(items):
        item=dict(original);item['raw_index']=item.get('raw_index',index)
        if pending and (item.get('page_idx')!=pending[0].get('page_idx')):
            for orphan in pending:
                orphan.update(type='image',image_caption=orphan.get('chart_caption',[]),normalization_flags=['ungrouped-chart'])
                result.append(orphan)
            pending=[]
        if item.get('type')=='chart':
            pending.append(item)
            captions=item.get('chart_caption',[])
            if any(re.match(r'\s*Fig(?:ure)?\.?\s*[A-Z]?\d+',c,re.I) for c in captions):
                item.update(type='image',image_caption=captions,source_panel_indices=[p['raw_index'] for p in pending],
                            bbox=[min(p['bbox'][0] for p in pending),min(p['bbox'][1] for p in pending),max(p['bbox'][2] for p in pending),max(p['bbox'][3] for p in pending)])
                result.append(item);pending=[]
            continue
        if item.get('type')=='ref_text':item.update(type='text',reference_source=True)
        result.append(item)
    for orphan in pending:
        orphan.update(type='image',image_caption=orphan.get('chart_caption',[]),normalization_flags=['ungrouped-chart'])
        result.append(orphan)
    return result


def table_crop(page,rect,raw,caption):
    crop=(rect+(-3,-3,3,3))&page.rect
    rules=[]
    for drawing in page.get_drawings():
        for item in drawing['items']:
            if item[0]=='l':
                a,z=item[1],item[2]
                if abs(a.y-z.y)<.6 and abs(a.x-z.x)>rect.width*.6 and rect.y0-1<=a.y<=rect.y0+8:rules.append(a.y)
    if rules:
        top=min(rules)-.7
        removed=native_region(raw,pymupdf.Rect(crop.x0,crop.y0,crop.x1,top))
        if not compact(removed) or compact(removed) in compact(caption):crop.y0=top
    return crop


def native_labels(raw):
    labels=[]
    for block in raw['blocks']:
        for line in block.get('lines',[]):
            text=''.join(c['c'] for span in line['spans'] for c in span['chars']).strip()
            if re.fullmatch(r'\([A-Z]?\d+\)',text) or re.match(r'^Table\s+[A-Z]?\d+\b',text):
                labels.append((text,pymupdf.Rect(line['bbox'])))
    return labels


def prepare(source, extract_json, work):
    source, extract_json, work = Path(source).resolve(), Path(extract_json).resolve(), Path(work).resolve()
    if (work / 'manifest.json').exists():
        raise ValueError('Work already prepared; reuse it or select a new directory')
    items = read(extract_json)
    if not isinstance(items, list) or not items:
        raise ValueError('Expected a nonempty MinerU content-list JSON array')
    report_path = extract_json.parent / 'extraction-report.json'
    extraction = read(report_path) if report_path.exists() else {'mode': 'provided-json'}
    if report_path.exists():
        if not extraction.get('success') or extraction.get('source_sha256') != digest(source):
            raise ValueError('Extraction report does not match the source PDF')
        artifact = next((a for a in extraction['artifacts']
                         if (extract_json.parent / a['path']).resolve() == extract_json), None)
        if artifact is None or artifact['sha256'] != digest(extract_json):
            raise ValueError('Structured extraction changed after download')
    items=normalize_items(items)
    blocks, ignored, raw_pages, labels = [], [], {}, {}
    work.mkdir(parents=True, exist_ok=True)
    (work / 'assets').mkdir(exist_ok=True)
    with pymupdf.open(source) as doc:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError('Malformed MinerU block')
            kind = item.get('type')
            if kind not in {'text', 'image', 'table', 'equation', 'header', 'footer',
                            'page_number', 'page_footnote'}:
                raise ValueError('Unsupported MinerU block type: ' + str(kind))
            page, rect = source_region(item, doc)
            if kind in {'header', 'footer', 'page_number'}:
                ignored.append({'index': index, 'source_page': item['page_idx'] + 1,
                                'type': kind, 'reason': 'running page furniture'})
                continue
            if page.number not in raw_pages:
                raw_pages[page.number] = page.get_text('rawdict', flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
                labels[page.number]=native_labels(raw_pages[page.number])
            native = native_region(raw_pages[page.number], rect)
            extracted = item.get('text', '')
            if not isinstance(extracted, str):
                raise ValueError('Malformed MinerU text')
            identity = hashlib.sha256(json.dumps([item, native], sort_keys=True,
                                                  ensure_ascii=False).encode()).hexdigest()[:12]
            block = {'id': f'M{index:04d}_{identity}', 'source_page': item['page_idx'] + 1,
                     'source_rect': list(rect), 'mineru_type': kind, 'mineru_index': index,
                     'type': 'note' if kind == 'page_footnote' else 'paragraph',
                     'source_text': native or extracted, 'mineru_text': extracted,
                     'review_flags': list(item.get('normalization_flags',[])),
                     'raw_index':item['raw_index']}
            if item.get('source_panel_indices'):block['source_panel_indices']=item['source_panel_indices']
            if item.get('reference_source'):block['reference_source']=True
            if kind == 'text' and item.get('text_level'):
                block['type'] = 'heading'
                block['level'] = max(1, min(4, int(item['text_level'])))
            if kind in {'text', 'page_footnote'}:
                if not extracted.strip() and native:
                    block['review_flags'].append('mineru-text-empty')
                if not block['source_text'].strip():
                    raise ValueError(f'Text region has no recoverable content: {block["id"]}')
            else:
                block['type'] = {'image': 'figure', 'table': 'table', 'equation': 'equation'}[kind]
                # Preserve scientific ink from the PDF, not reconstructed OCR formulas.
                captions = item.get('image_caption', []) if kind == 'image' else item.get('table_caption', [])
                if not isinstance(captions,list) or any(not isinstance(c,str) for c in captions):raise ValueError('Malformed media caption')
                if kind=='table':
                    candidates=[(text,r) for text,r in labels[page.number] if text.startswith('Table') and 0<=rect.y0-r.y1<65 and r.x0<=rect.x1 and r.x1>=rect.x0]
                    if candidates:
                        text,r=min(candidates,key=lambda v:rect.y0-v[1].y1)
                        number=re.match(r'^Table\s+([A-Z]?\d+)',text)[1]
                        cap=re.sub(r'^Table\s+[A-Z]?\d+\s*[.:]?\s*','',' '.join(captions)).strip()
                        captions=[f'Table {number}. '+cap];block['number_source']='native PDF caption'
                crop = table_crop(page,rect,raw_pages[page.number],' '.join(captions)) if kind=='table' else (rect + (-3, -3, 3, 3)) & page.rect
                asset = work / 'assets' / (block['id'] + '.png')
                page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=crop, alpha=False).save(asset)
                block.update(asset=asset.relative_to(work).as_posix(), asset_sha256=digest(asset),
                             width_pt=crop.width, height_pt=crop.height,crop_rect=list(crop))
                if not isinstance(captions, list) or any(not isinstance(c, str) for c in captions):
                    raise ValueError('Malformed media caption')
                block['source_text'] = '\n'.join(captions)
                block['mineru_text'] = block['source_text'] if kind != 'equation' else extracted
                if kind == 'equation':
                    label = re.search(r'\\tag\s*\{([^{}]+)\}', extracted)
                    block['number'] = label[1] if label else None
                    candidates=[(text,r) for text,r in labels[page.number] if re.fullmatch(r'\([A-Z]?\d+\)',text) and rect.y0-7<=(r.y0+r.y1)/2<=rect.y1+7 and r.x0>=rect.x0 and ((r.x0<page.rect.width/2)==(rect.x0<page.rect.width/2) or rect.width>page.rect.width/2)]
                    if candidates:
                        text,r=min(candidates,key=lambda v:abs((v[1].y0+v[1].y1-rect.y0-rect.y1)/2))
                        block['number']=text[1:-1];block['number_source']='native PDF label'
                else:
                    number=re.match(r'\s*(?:Fig(?:ure)?\.?|Table)\s*([A-Z]?\d+)',' '.join(captions),re.I)
                    if number:block['number']=number[1]
            blocks.append(block)
        for parts in reflow_links.references(blocks).values():
            for block in parts:block['reference_source']=True
        manifest = {'schema_version': 'mineru-reflow-1', 'source_pdf': str(source),
                    'source_sha256': digest(source), 'source_pages': len(doc),
                    'extract_json': str(extract_json), 'extract_sha256': digest(extract_json),
                    'extraction': {k: extraction.get(k) for k in
                                   ['mode', 'model', 'cli_version', 'elapsed_seconds', 'success']},
                    'blocks': blocks, 'ignored': ignored,
                    'pending_jobs': [{'id': b['id'], 'type': b['type'], 'source_page': b['source_page'],
                                      'source_text': b['source_text'], 'mineru_text': b['mineru_text'],
                                      'review_flags': b['review_flags']}
                                     for b in blocks if b['source_text'].strip() and not b.get('reference_source')],
                    'layout': {'body_pt': 9.5, 'line_height': 1.45, 'page': 'A4', 'margin_pt': 38}}
    write(work / 'manifest.json', manifest)
    write(work / 'pending-jobs.json', manifest['pending_jobs'])
    return manifest


CSS = '''body {font-family:sans-serif;font-size:9.5pt;line-height:1.5;color:#20262d;}
p {margin:0 0 7pt;text-align:justify;}
h1 {font-size:17pt;line-height:1.3;margin:0 0 14pt;page-break-after:avoid;}
h2 {font-size:12pt;margin:18pt 0 8pt;page-break-after:avoid;}
h3,h4 {font-size:10.5pt;margin:13pt 0 6pt;page-break-after:avoid;}
.source {font-size:7pt;color:#687785;margin:2pt 0 3pt;text-align:right;}
.media {text-align:center;margin:10pt 0;page-break-inside:avoid;}
.caption {font-size:8.2pt;line-height:1.4;text-align:justify;margin-top:6pt;}
.note {font-size:8pt;line-height:1.4;color:#4b5563;}
sup,sub {font-size:70%;} a {color:#2196d1;text-decoration:none;}
'''


def render_document(body, work, media_specs):
    """Move media to a fresh page if MuPDF silently shrinks it into a short gap."""
    pagebox = pymupdf.paper_rect('a4')
    bounds = pagebox + (60, 50, -60, -50)
    breaks = []
    for attempt in range(len(media_specs) + 1):
        positions = []
        story = pymupdf.Story(body, user_css=CSS, em=9.5, archive=pymupdf.Archive(str(work)))

        def position(pos):
            if getattr(pos, 'id', None):
                positions.append({'id': pos.id, 'page': pos.page_num, 'rect': list(pos.rect)})

        def rectfn(rect_number, filled):
            if rect_number > 1000:
                raise ValueError('Pagination made no bounded progress')
            return pagebox, bounds, None

        pdf = story.write_with_links(rectfn, positionfn=position)
        actual_by_digest = {}
        for index, page in enumerate(pdf):
            for info in page.get_image_info(hashes=True):
                rect = pymupdf.Rect(info['bbox'])
                actual_by_digest.setdefault(info['digest'].hex(), []).append(
                    {'page': index + 1, 'width': rect.width, 'height': rect.height})
        bad, placements = [], []
        for spec in media_specs:
            matches = actual_by_digest.get(spec['digest'], [])
            if not matches:
                pdf.close()
                raise ValueError('Original media missing from PDF: ' + spec['id'])
            actual = matches.pop(0)
            placements.append({**spec, 'actual': actual})
            if actual['width'] < spec['width'] * .99 or actual['height'] < spec['height'] * .99:
                bad.append(spec['id'])
        if not bad:
            return pdf, positions, placements, breaks, body, bounds
        pdf.close()
        if any(ident in breaks for ident in bad):
            raise ValueError('Media cannot be rendered at readable size: ' + ', '.join(bad))
        for ident in bad:
            body = body.replace(f'<div class="media" id="{ident}">',
                                f'<div class="media" id="{ident}" style="page-break-before:always">', 1)
        breaks.extend(bad)
    raise ValueError('Media pagination did not converge')


COLUMN_CSS = '''
@page {size:A4;margin:40pt 38pt 44pt;}
html,body {margin:0;padding:0;}
body {font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-size:9.5pt;line-height:1.45;color:#20262d;}
.columns {column-count:2;column-gap:22pt;column-fill:auto;}
p {margin:0 0 4pt;text-align:justify;text-indent:2em;orphans:3;widows:3;overflow-wrap:break-word;}
h1 {font-size:17pt;line-height:1.3;margin:5pt 0 10pt;column-span:all;}
h2 {font-size:11pt;margin:12pt 0 6pt;line-height:1.3;break-after:avoid;}
h3,h4 {font-size:10pt;margin:10pt 0 5pt;line-height:1.3;break-after:avoid;}
.article-head {margin-bottom:12pt;}
.article-head p {font-size:7.5pt;line-height:1.3;margin-bottom:2pt;}
.article-head h2 {font-size:10pt;margin:9pt 0 4pt;}
.article-head p.abstract {font-size:9pt;line-height:1.4;margin-top:3pt;}
.source {font-size:7pt;color:#687785;text-align:right;margin:1pt 0 3pt;}
.source {display:none}h1,h2,h3,h4{font-family:"Microsoft YaHei",sans-serif}.caption,.note,.article-head p{text-indent:0}
.media {text-align:center;margin:9pt 0;break-inside:avoid;page-break-inside:avoid;}
.media.wide {column-span:all;margin:12pt 0;}
.media img {display:inline-block;max-width:100%;vertical-align:middle;}
.media.equation {display:flex;align-items:center;justify-content:center;gap:8pt;margin:5pt 0;}
.eq-number {font-family:serif;font-size:8.2pt;white-space:nowrap;}
.caption {font-size:8.2pt;line-height:1.4;text-align:justify;margin:5pt 0 0;}
.note {font-size:7.5pt;line-height:1.35;color:#4b5563;}
sup,sub {font-size:70%;line-height:0;} a {color:#2196d1;text-decoration:none;}
'''


def media_at_readable_size(actual, expected):
    # Chromium rounds image edges to CSS pixels (0.75pt); retain real shrink checks.
    return all(actual[axis] + .75 >= expected[axis] * .99 for axis in ('width', 'height'))


def render_columns(parts, groups, blocks, scope, work, media_specs, *, translations=None, reviews=None):
    """Use the installed local browser for paged columns and true spanning figures."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError('Double columns need local Playwright and Chromium; no remote renderer is used') from exc
    by_id = {b['id']: b for b in blocks}
    start = 0
    for group in groups:
        block = by_id[group['ids'][0]]
        label = compact(block.get('mineru_text') or block['source_text']).lower()
        if (group['kind'] == 'heading' and block.get('level', 2) > 1
                and label not in {'abstract', '摘要', 'articleinfo', 'keywords'}):
            start = group['html_index']
            break
    for group in groups:
        block=by_id[group['ids'][0]]
        label=compact(block.get('mineru_text') or block['source_text']).lower()
        if group['kind']=='heading' and (re.match(r'^1[.、]',label) or label in {'introduction','引言','绪论'}):
            start=group['html_index'];break
    head = ''.join(parts[:start])
    for index, group in enumerate(groups[:-1]):
        block = by_id[group['ids'][0]]
        if compact(block.get('mineru_text') or block['source_text']).lower() in {'abstract', '摘要'}:
            ident = groups[index + 1]['ids'][0]
            head = head.replace(f'<p id="{ident}">', f'<p class="abstract" id="{ident}">', 1)
    body = (f'<html><head><meta charset="utf-8"><style>{COLUMN_CSS}</style></head><body>'
            + scope + '<header class="article-head">' + head + '</header><main class="columns">'
            + ''.join(parts[start:]) + '</main></body></html>')
    for block in blocks:
        if 'asset' in block and f'src="{block["asset"]}"' in body:
            asset = work / block['asset']
            uri = 'data:image/png;base64,' + base64.b64encode(asset.read_bytes()).decode('ascii')
            body = body.replace(f'src="{block["asset"]}"', f'src="{uri}"')
    body = reflow_links.style_print_citations(body, blocks, translations or {}, reviews or {}, color='#2196d1')
    with sync_playwright() as runtime:
        with runtime.chromium.launch(headless=True) as browser:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content(body, wait_until='load')
            page.evaluate('document.fonts.ready')
            data = page.pdf(print_background=True, prefer_css_page_size=True, tagged=True)
            # Chromium can leave a spanning media object beyond the page bottom.
            # One concentrated pagination repair is allowed; never hide overflow.
            check=pymupdf.open(stream=data,filetype='pdf');bad_digests=set()
            for sheet in check:
                for item in sheet.get_image_info(hashes=True):
                    if item['bbox'][3]>sheet.rect.height-43:bad_digests.add(item['digest'].hex())
            check.close()
            repaired=[s['id'] for s in media_specs if s['digest'] in bad_digests]
            if repaired:
                rules=''.join('#'+ident+'{break-before:page}' for ident in repaired)
                body=body.replace('</head>','<style>'+rules+'</style></head>')
                page.set_content(body,wait_until='load');page.evaluate('document.fonts.ready')
                data=page.pdf(print_background=True,prefer_css_page_size=True,tagged=True)
    pdf = pymupdf.open(stream=data, filetype='pdf')
    positions, actual_by_digest = [], {}
    for index, page in enumerate(pdf):
        for block in page.get_text('dict')['blocks']:
            for line in block.get('lines', []):
                for span in line['spans']:
                    # Font ascender boxes can extend 1-2pt above an intact superscript.
                    positions.append({'id': 'text-span', 'page': index + 1, 'rect': list(span['bbox']),
                                      'tolerance_pt': 2})
        for info in page.get_image_info(hashes=True):
            rect = pymupdf.Rect(info['bbox'])
            positions.append({'id': 'media', 'page': index + 1, 'rect': list(rect), 'tolerance_pt': .75})
            actual_by_digest.setdefault(info['digest'].hex(), []).append(
                {'page': index + 1, 'width': rect.width, 'height': rect.height})
    placements = []
    for spec in media_specs:
        matches = actual_by_digest.get(spec['digest'], [])
        if not matches:
            pdf.close()
            raise ValueError('Original media missing from double-column PDF: ' + spec['id'])
        actual = matches.pop(0)
        if not media_at_readable_size(actual, spec):
            (work / 'double-column-layout.failed.pdf').write_bytes(data)
            pdf.close()
            raise ValueError(f'Media unexpectedly reduced in double-column PDF: {spec["id"]}; '
                             f'expected {spec["width"]:.2f} x {spec["height"]:.2f} pt, actual {actual}')
        placements.append({**spec, 'actual': actual})
    return pdf, positions, placements, repaired, body, pymupdf.paper_rect('a4') + (38, 40, -38, -44)


def finish(work, translations, output, reviews=None, layout='single', html_output=None):
    if layout not in {'single', 'double'}:
        raise ValueError('Layout must be single or double')
    work, output = Path(work).resolve(), Path(output).resolve()
    manifest, reviews = read(work / 'manifest.json'), {k:dict(v) for k,v in (reviews or {}).items()}
    if digest(manifest['source_pdf']) != manifest['source_sha256']:
        raise ValueError('Source PDF changed after prepare')
    blocks = manifest['blocks']
    for parts in reflow_links.references(blocks).values():
        for block in parts:reviews.setdefault(block['id'],{}).setdefault('preserve',True)
    unknown = (set(translations) | set(reviews)) - {b['id'] for b in blocks}
    if unknown:
        raise ValueError('Unknown translation/review IDs: ' + ', '.join(sorted(unknown)))
    for ident,review in reviews.items():
        target=review.get('translation_in')
        if target and (target==ident or target not in {b['id'] for b in blocks} or reviews.get(target,{}).get('translation_in') or not translations.get(target) or not review.get('note')):
            raise ValueError('Invalid reviewed whole-paragraph translation group: '+ident)
    missing = [b['id'] for b in blocks if b['source_text'].strip()
               and not reviews.get(b['id'], {}).get('omit_reason')
               and not reviews.get(b['id'], {}).get('preserve')
               and not reviews.get(b['id'], {}).get('translation_in')
               and not str(translations.get(b['id'], '')).strip()]
    if missing:
        raise ValueError('Missing translations: ' + ', '.join(missing))
    rendered, omitted, parts, notes, expected, groups, media_specs = [], [], [], [], [], [], []
    source_seen = set()
    for block in blocks:
        ident, kind = block['id'], block['type']
        review = reviews.get(ident, {})
        if review.get('omit_reason'):
            omitted.append({'id': ident, 'reason': review['omit_reason']})
            continue
        if block['review_flags'] and not str(review.get('note', '')).strip():
            raise ValueError('Source discrepancy requires a review note: ' + ident)
        if review.get('translation_in'):
            target=review['translation_in'];group=next((g for g in groups if target in g['ids']),None)
            if group is None or group['kind']!='paragraph':raise ValueError('Whole paragraph must be rendered at its first source ID: '+ident)
            group['ids'].append(ident)
            i=group['html_index'];parts[i]=parts[i].replace(f'id="{target}">',f'id="{target}"><span id="{ident}"></span>',1)
            rendered.append(ident);continue
        text = block['source_text'] if review.get('preserve') else translations.get(ident, '')
        if not isinstance(text, str) or re.search(r'⟦V4_|\$|\\(?:frac|mathrm|mathbf|left|right)\b', text):
            raise ValueError('Unrendered formula or invalid translation: ' + ident)
        rendered.append(ident)
        if text.strip():
            expected.append((ident, compact(text)))
        source_label = ''
        if kind != 'note' and block['source_page'] not in source_seen and not (review.get('join_previous') or review.get('join_to')):
            source_label = f'<p class="source">原文第 {block["source_page"]} 页</p>'
            source_seen.add(block['source_page'])
        if 'asset' in block:
            asset = (work / block['asset']).resolve()
            if (not asset.is_relative_to(work) or asset.suffix != '.png'
                    or not asset.exists() or digest(asset) != block['asset_sha256']):
                raise ValueError('Missing or changed original media: ' + ident)
            wide = layout == 'double' and kind != 'equation' and block['width_pt'] > 247
            max_width = 500 if wide else 215 if layout == 'double' else 450
            scale = min(1.3 if kind == 'equation' else 1.0, max_width / block['width_pt'],
                        440 / block['height_pt'])
            width, height = block['width_pt'] * scale, block['height_pt'] * scale
            media_specs.append({'id': ident, 'digest': pymupdf.Pixmap(str(asset)).digest.hex(),
                                'width': width, 'height': height})
            label = f'　({html.escape(str(block["number"]))})' if block.get('number') else ''
            if layout == 'double' and label:
                label = f'<span class="eq-number">{label.strip()}</span>'
            classes = 'media' if layout == 'single' else 'media ' + kind + (' wide' if wide else '')
            caption = safe_markup(text)
            if layout == 'double':
                caption = re.sub(r'^(图|表)(\s*\d+)', r'<b>\1\2</b>', caption)
            content = (source_label + f'<div class="{classes}" id="{ident}"><img src="{block["asset"]}" '
                       f'style="width:{width:.2f}pt;height:{height:.2f}pt">{label}'
                       + (f'<p class="caption">{caption}</p>' if text else '') + '</div>')
            parts.append(content)
            groups.append({'ids': [ident], 'html_index': len(parts) - 1, 'kind': kind})
        elif kind == 'note':
            notes.append(f'<p class="note" id="{ident}">{safe_markup(text)}</p>')
        elif review.get('join_previous') or review.get('join_to'):
            group=next((g for g in groups if review.get('join_to') in g['ids']),None) if review.get('join_to') else (groups[-1] if groups else None)
            if group is None or group['kind'] != 'paragraph':
                raise ValueError('join_previous requires a preceding paragraph: ' + ident)
            group['ids'].append(ident)
            parts[group['html_index']] = parts[group['html_index']][:-4] + f' <span id="{ident}">' + safe_markup(text) + '</span></p>'
        else:
            tag = 'h' + str(block.get('level', 2)) if kind == 'heading' else 'p'
            parts.append(source_label + f'<{tag} id="{ident}">{safe_markup(text)}</{tag}>')
            groups.append({'ids': [ident], 'html_index': len(parts) - 1, 'kind': kind})
    if notes:
        parts.append('<h2>原文附注</h2>' + ''.join(notes))
    scope_note = manifest.get('scope_note', '')
    scope = f'<p class="note">{safe_markup(scope_note)}</p>' if scope_note else ''
    body = '<html><head><meta charset="utf-8"></head><body>' + scope + ''.join(parts) + '</body></html>'
    if layout == 'double':
        pdf, positions, placements, breaks, body, bounds = render_columns(parts, groups, blocks, scope, work, media_specs, translations=translations, reviews=reviews)
    else:
        pdf, positions, placements, breaks, body, bounds = render_document(body, work, media_specs)
    (work / 'reading.html').write_text('<style>' + CSS + '</style>' + body, encoding='utf-8')
    try:
        pdf_text = compact(''.join(p.get_text() for p in pdf))
        lost = [ident for ident, text in expected if text and text not in pdf_text]
        if lost:
            raise ValueError('Rendered PDF text missing or altered: ' + ', '.join(lost))
        overflow = [p for p in positions if p['rect'][0] < bounds.x0 - p.get('tolerance_pt', 1)
                    or p['rect'][2] > bounds.x1 + p.get('tolerance_pt', 1)
                    or p['rect'][1] < bounds.y0 - p.get('tolerance_pt', 1)
                    or p['rect'][3] > bounds.y1 + p.get('tolerance_pt', 1)]
        if overflow:
            raise ValueError('Rendered block outside page text area')
        navigation=reflow_links.attach(pdf,blocks,translations,reviews,manifest['source_pdf'],work)
        reading=reflow_links.decorate_html(body,blocks,translations,reviews,navigation,work)
        (work/'reading.html').write_text(reading,encoding='utf-8')
        if html_output:
            html_path=Path(html_output).resolve();html_path.parent.mkdir(parents=True,exist_ok=True)
            html_path.write_text(reading,encoding='utf-8')
        for index, page in enumerate(pdf):
            page.insert_text((60, page.rect.height - 26), f'{index + 1} / {len(pdf)}', fontsize=8, color=(.4, .45, .5))
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(output.name + '.tmp')
        page_count = len(pdf)
        pdf.save(temp, garbage=4, deflate=True)
        pdf.close()
        temp.replace(output)
        report = {'status': 'passed', 'pages': page_count, 'output': str(output),
                  'engine': {'root': str(Path(__file__).resolve().parents[1]), 'revision': 'mineru-method-3-bidirectional'},
                  'output_sha256': digest(output), 'source_sha256': manifest['source_sha256'],
                  'layout': {**manifest['layout'], 'columns': 2 if layout == 'double' else 1,
                             'line_height': 1.45 if layout == 'double' else 1.5,
                             'margin_pt': 38 if layout == 'double' else 60,
                             'vertical_margin_pt': [40, 44] if layout == 'double' else [50, 50]},
                  'extractor': manifest['extraction'],
                  'rendered_block_ids': rendered, 'omitted': omitted, 'groups': groups,
                  'positions': positions, 'text_preservation': 'all translated/preserved blocks found',
                  'media_preservation': 'hashed original-PDF crops',
                  'media_placements': placements, 'media_page_breaks': breaks,'navigation':navigation,
                  'merged_translations':{k:v['translation_in'] for k,v in reviews.items() if v.get('translation_in')},
                  'scope': 'Selected source pages only; human source and layout review required'}
        write(work / 'delivery.json', report)
        return report
    finally:
        if not pdf.is_closed:
            pdf.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--source', required=True)
    prep.add_argument('--work', required=True)
    prep.add_argument('--extract-json', help='Existing matching MinerU content-list JSON')
    prep.add_argument('--mineru-cli')
    done = sub.add_parser('finish')
    done.add_argument('--work', required=True)
    done.add_argument('--translations', required=True)
    done.add_argument('--reviews', help='Reviewed preserve/omit/join decisions by block ID')
    done.add_argument('--output', required=True)
    done.add_argument('--html-output',help='Optional offline HTML using the same translations and bidirectional reference numbers')
    done.add_argument('--layout', choices=['double', 'single'], default='double', help='Double columns preferred; single retained on request')
    args = parser.parse_args()
    if args.command == 'prepare':
        if (Path(args.work) / 'manifest.json').exists():
            raise SystemExit('Work already prepared; resume with pending-jobs.json / finish, or select a new directory')
        if not args.extract_json:
            command = [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('mineru_extract.py')),
                       '--source', args.source, '--output', str(Path(args.work) / 'extraction'),
                       '--mode', 'precision', '--model', 'vlm', '--structured']
            if args.mineru_cli:
                command += ['--cli', args.mineru_cli]
            run = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
            if run.returncode:
                raise SystemExit('MinerU extraction failed; preserve TLS and inspect extraction-report.json')
            cache = Path(json.loads(run.stdout.strip())['output'])
            found = [p for p in cache.glob('*.json') if p.name != 'extraction-report.json']
            if len(found) != 1:
                raise SystemExit('Expected exactly one MinerU content-list JSON')
            args.extract_json = str(found[0])
        manifest = prepare(args.source, args.extract_json, args.work)
        print(json.dumps({'blocks': len(manifest['blocks']), 'jobs': len(manifest['pending_jobs']),
                          'review_flags': sum(bool(b['review_flags']) for b in manifest['blocks']),
                          'work': args.work}, ensure_ascii=False))
    else:
        result = finish(args.work, read(args.translations), args.output, read(args.reviews) if args.reviews else None, args.layout,args.html_output)
        print(json.dumps({k: result[k] for k in ['status', 'pages', 'output']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
