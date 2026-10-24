"""MinerU structure fused onto PyMuPDF glyphs for one source map.

MinerU decides block kind, paragraph grouping and reading order. When the page
has a text layer, redaction rectangles and font metrics stay on the PyMuPDF
spans. A scanned region with only MinerU OCR text becomes a source-map block
from that text and the denormalized box.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .classifier import adapted_body_size, intersects
from .paragraphs import link_paragraphs, load_mineru_items
from .pdf_reader import stable_id


_CAPTION = re.compile(r'^(?:Fig(?:ure)?\.?|Table)\s*[A-Z]?\d+', re.I)
_SECRET = re.compile(r'(?i)(token|api[_ ]?key|authorization|bearer)([=: ]+)\S+')


def normalize_items(items):
    """Adapt chart groups and reference rows without mutating the service JSON."""
    result, pending = [], []
    for index, original in enumerate(items):
        item = dict(original)
        item['raw_index'] = item.get('raw_index', index)
        if pending and item.get('page_idx') != pending[0].get('page_idx'):
            for orphan in pending:
                orphan.update(type='image', image_caption=orphan.get('chart_caption', []),
                              normalization_flags=['ungrouped-chart'])
                result.append(orphan)
            pending = []
        if item.get('type') == 'chart':
            pending.append(item)
            captions = item.get('chart_caption', [])
            if any(re.match(r'\s*Fig(?:ure)?\.?\s*[A-Z]?\d+', c, re.I) for c in captions):
                item.update(type='image', image_caption=captions,
                            source_panel_indices=[p['raw_index'] for p in pending],
                            bbox=[min(p['bbox'][0] for p in pending), min(p['bbox'][1] for p in pending),
                                  max(p['bbox'][2] for p in pending), max(p['bbox'][3] for p in pending)])
                result.append(item)
                pending = []
            continue
        if item.get('type') == 'ref_text':
            item.update(type='text', reference_source=True)
        result.append(item)
    for orphan in pending:
        orphan.update(type='image', image_caption=orphan.get('chart_caption', []),
                      normalization_flags=['ungrouped-chart'])
        result.append(orphan)
    return result


def _windows_token():
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'mineru_extract.py'
    if not script.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location('mineru_extract_token', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.stored_token()


def mineru_token():
    """Return a configured MinerU credential, or None. Never log the value."""
    for name in ('MINERU_API_KEY', 'MINERU_TOKEN'):
        value = os.environ.get(name, '').strip()
        if value:
            return value
    file_name = os.environ.get('MINERU_TOKEN_FILE', '').strip()
    candidates = [Path(file_name)] if file_name else []
    candidates.append(Path.home() / '.config' / 'pdf-chinese-reader' / 'mineru-token')
    for path in candidates:
        try:
            text = path.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if text:
            return text
    try:
        return _windows_token()
    except Exception:
        return None


def mineru_configured():
    return bool(mineru_token())


def _redact(text):
    return _SECRET.sub(r'\1\2<redacted>', text or '')


def extract_with_mineru(source, output, mineru_cli=None):
    """Run the local MinerU CLI. Returns (ok, detail). Detail never contains a token."""
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'mineru_extract.py'
    command = [sys.executable, '-X', 'utf8', str(script), '--source', str(source),
               '--output', str(output), '--mode', 'precision', '--structured', '--ocr']
    if mineru_cli:
        command += ['--cli', str(mineru_cli)]
    env = os.environ.copy()
    token = mineru_token()
    if token and not env.get('MINERU_TOKEN'):
        env['MINERU_TOKEN'] = token
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', env=env)
    except OSError as exc:
        return False, f'MinerU extraction could not start: {exc}'
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or 'MinerU extraction failed').strip()
        return False, _redact(detail)[:500]
    return True, ''


def stage_content_list(work, extract_json):
    """Copy an already downloaded content list where prepare looks for it."""
    dest = Path(work) / 'extraction'
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(extract_json, dest / 'content_list.json')


def _denorm(bbox, bounds):
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    rect = [bounds[0] + bbox[0] * width / 1000, bounds[1] + bbox[1] * height / 1000,
            bounds[0] + bbox[2] * width / 1000, bounds[1] + bbox[3] * height / 1000]
    rect = [max(bounds[0], rect[0]), max(bounds[1], rect[1]), min(bounds[2], rect[2]), min(bounds[3], rect[3])]
    return rect


def _valid_bbox(bbox):
    return (isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(n, (int, float)) for n in bbox)
            and bbox[0] < bbox[2] and bbox[1] < bbox[3])


def _compact(text):
    return re.sub(r'\s+', '', re.sub(r'<[^>]+>', '', text or '')).lower()


def _overlap_frac(rect, box):
    width = min(rect[2], box[2]) - max(rect[0], box[0])
    height = min(rect[3], box[3]) - max(rect[1], box[1])
    area = max(rect[2] - rect[0], 0.01) * max(rect[3] - rect[1], 0.01)
    if width <= 0 or height <= 0:
        return 0
    return width * height / area


def _center_in(rect, box):
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _captions(item):
    values = item.get('image_caption') if item.get('type') in {'image', 'chart'} else item.get('table_caption')
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def _is_caption(text, captions):
    plain = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or '')).strip()
    if _CAPTION.match(plain):
        return True
    folded = _compact(plain)
    return any(folded and (_compact(caption) in folded or folded in _compact(caption)) for caption in captions)


def _apply_structure(block, item):
    """Retype a native block. Its rectangle and source text stay PyMuPDF's."""
    kind = item.get('type')
    if item.get('reference_source'):
        block.update(type='reference', translation_required=False, preservation_reason='bibliography')
        return
    if kind == 'equation':
        block.update(type='formula', translation_required=False, preservation_reason='mineru-formula')
        return
    captions = _captions(item)
    if kind in {'table', 'image'} and _is_caption(block.get('source_text', ''), captions):
        block['type'] = 'caption'
        block['translation_required'] = True
        block.pop('preservation_reason', None)
        return
    if kind == 'table':
        block.update(type='table', translation_required=False, preservation_reason='mineru-table')
        return
    if kind == 'image':
        block.update(type='figure', translation_required=False, preservation_reason='mineru-figure')
        return
    level = item.get('text_level')
    if level:
        plain = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', block.get('source_text', ''))).strip()
        block['type'] = 'title' if int(level) == 1 and not re.match(r'\d', plain) else 'heading'
        block['translation_required'] = True
        block.pop('preservation_reason', None)
        return
    if kind in {'text', 'page_footnote'} and (
            block.get('type') == 'formula'
            or block.get('preservation_reason') == 'native-micro-equation-or-formula'):
        block['type'] = 'paragraph'
        block['translation_required'] = True
        block.pop('preservation_reason', None)


def _hits(blocks, box, kind, blob):
    if kind in {'equation', 'table', 'image'}:
        return [block for block in blocks if _overlap_frac(block['rect'], box) >= 0.5]
    geometric = [block for block in blocks if _center_in(block['rect'], box) or _overlap_frac(block['rect'], box) >= 0.45]
    if not blob:
        return geometric
    contained = [block for block in geometric if _compact(block.get('source_text', '')) and _compact(block.get('source_text', '')) in blob]
    return contained or geometric


def _ocr_block(page, rect, text, kind, item, index):
    captions = _captions(item)
    if kind in {'image', 'table'}:
        block_type, translatable = ('caption', True) if captions or _CAPTION.match(text) else (
            'table' if kind == 'table' else 'figure', False)
    elif kind == 'equation':
        block_type, translatable = 'formula', False
    elif item.get('text_level'):
        block_type, translatable = ('title' if int(item['text_level']) == 1 and not re.match(r'\d', text) else 'heading', True)
    elif item.get('reference_source'):
        block_type, translatable = 'reference', False
    else:
        block_type, translatable = ('note' if kind == 'page_footnote' else 'paragraph', True)
    block = {'id': stable_id(page, rect, text, 'O'), 'source_page': page, 'rect': [round(v, 2) for v in rect],
             'source_text': text, 'source_text_sha256': hashlib.sha256(text.encode()).hexdigest(),
             'type': block_type, 'translation_required': translatable, 'source_font_pt': 9.0,
             'size': adapted_body_size(9) if translatable else 9, 'min_scale': 0.85, 'leading': 1.17,
             'origin': 'mineru-ocr', 'original_ids': [], 'mineru_reading_index': index,
             'mineru_type': kind, 'source_lines': []}
    if not translatable:
        block['preservation_reason'] = 'mineru-ocr-' + str(kind)
    return block


def _maybe_protect(page, box, kind, blocks):
    if any(block.get('translation_required', True) and intersects(block['rect'], box) for block in blocks):
        return
    page.setdefault('protected', []).append({
        'id': stable_id(page['source_page'], box, kind, 'P'), 'rect': [round(v, 2) for v in box],
        'type': 'figure' if kind == 'image' else 'table' if kind == 'table' else 'formula'})


def fuse_mineru(mapping, extraction_dir):
    """Apply MinerU structure to a classified source map. Returns a small report."""
    items = normalize_items(load_mineru_items(extraction_dir))
    if not items:
        return {'applied': False, 'reason': 'MinerU extraction produced no content list.'}
    pages = {page['source_page']: page for page in mapping.get('pages', [])}
    claimed = set()
    order = 0
    added = []
    ocr_pages = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get('type')
        if kind in {'header', 'footer', 'page_number'}:
            continue
        page_index = item.get('page_idx')
        if not isinstance(page_index, int) or page_index + 1 not in pages or not _valid_bbox(item.get('bbox')):
            continue
        page_number = page_index + 1
        box = _denorm(item['bbox'], pages[page_number]['rect'])
        if box[2] - box[0] < 0.5 or box[3] - box[1] < 0.5:
            continue
        blob = _compact(item.get('text') or ' '.join(_captions(item)))
        hits = _hits([block for block in mapping['blocks']
                      if block.get('source_page') == page_number and block['id'] not in claimed],
                     box, kind, blob)
        if hits:
            hits.sort(key=lambda block: (block['rect'][1], block['rect'][0], block['id']))
            for offset, block in enumerate(hits):
                claimed.add(block['id'])
                block['mineru_reading_index'] = order + offset / 1000
                block['mineru_type'] = kind
                _apply_structure(block, item)
            if kind in {'image', 'table', 'equation'}:
                _maybe_protect(pages[page_number], box, kind, mapping['blocks'])
            order += 1
            continue
        text = item.get('text') if isinstance(item.get('text'), str) else ''
        if kind in {'image', 'table'}:
            text = '\n'.join(_captions(item)) or text
        text = text.strip()
        if not text:
            if kind in {'image', 'table', 'equation'}:
                _maybe_protect(pages[page_number], box, kind, mapping['blocks'])
                order += 1
            continue
        added.append(_ocr_block(page_number, box, text, kind, item, order))
        ocr_pages.add(page_number)
        if kind in {'image', 'table', 'equation'}:
            _maybe_protect(pages[page_number], box, kind, mapping['blocks'] + added)
        order += 1
    if added:
        mapping['blocks'].extend(added)
        mapping['issues'] = [issue for issue in mapping.get('issues', [])
                             if not (issue.get('check') == 'no-extractable-text' and issue.get('page') in ocr_pages)]
    mapping['expected_block_ids'] = [block['id'] for block in mapping['blocks']]
    link_paragraphs(mapping, extraction_dir)
    return {'applied': True, 'items': len(items), 'ocr_blocks': len(added), 'claimed_blocks': len(claimed)}
