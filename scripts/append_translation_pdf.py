"""Append per-source-page Chinese translation pages while preserving every source page."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time

import pymupdf as fitz
from pdf_layout import validate_layout


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def plain_text(page):
    return page.get_text('text')


def insert_header(page, source_page, fontfile, continuation=False):
    suffix = '（续）' if continuation else ''
    page.insert_text((50, 32), f'原文第{source_page}页 · 中文译文{suffix}',
                     fontname='zh', fontfile=fontfile, fontsize=9, color=(.25, .32, .4))
    page.draw_line((50, 45), (page.rect.width - 50, 45), color=(.6, .68, .74), width=.7)
    return 60


def add_return_link(page, source_output_page, fontfile):
    label = '返回原文'
    width = 42  # Four CJK glyphs plus a small click-target margin.
    x = page.rect.width - 50 - width
    page.insert_text((x, 32), label, fontname='zh', fontfile=fontfile,
                     fontsize=9, color=(0, .38, .55))
    page.insert_link({'kind': fitz.LINK_GOTO,
                      'from': fitz.Rect(x - 2, 18, x + width + 2, 40),
                      'page': source_output_page, 'to': fitz.Point(0, 0)})


def text_height(text, width, size, lineheight):
    # CJK glyphs occupy about one em; Latin and digits average about half an em.
    units = sum(1.0 if '\u3400' <= ch <= '\u9fff' else .52 for ch in text)
    lines = max(1, math.ceil(units * size / max(width, 1)))
    return lines * size * lineheight + 8


def add_text(page, text, y, fontfile, size=10.5, lineheight=1.45):
    width = page.rect.width - 104
    height = text_height(text, width, size, lineheight)
    top = y
    while top + height > page.rect.height - 48:
        raise OverflowError('Block does not fit on an empty translation page')
    rect = fitz.Rect(54, top, page.rect.width - 50, top + height)
    spare = page.insert_textbox(rect, text, fontfile=fontfile, fontname='zh', fontsize=size,
                                lineheight=lineheight, color=(.08, .08, .08), overlay=True)
    while spare < 0:
        height += max(12, -spare + 4)
        if top + height > page.rect.height - 48:
            raise OverflowError('Text block is too long for one page; split it in the manifest')
        rect = fitz.Rect(54, top, page.rect.width - 50, top + height)
        spare = page.insert_textbox(rect, text, fontfile=fontfile, fontname='zh', fontsize=size,
                                    lineheight=lineheight, color=(.08, .08, .08), overlay=True)
    return rect.y1 - spare + 8


def add_crop(page, source, crop_spec, y, max_height=180):
    source_page = int(crop_spec['source_page'])
    if not 1 <= source_page <= len(source):
        raise ValueError(f'Invalid crop source page: {source_page}')
    rect = fitz.Rect(crop_spec['rect'])
    if rect.is_empty or not source[source_page - 1].rect.contains(rect):
        raise ValueError(f'Crop rectangle is outside source page {source_page}: {list(rect)}')
    pix = source[source_page - 1].get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
    max_width = page.rect.width - 110
    scale = min(max_width / pix.width, max_height / pix.height, 1.0)
    width, height = pix.width * scale, pix.height * scale
    target = fitz.Rect((page.rect.width - width) / 2, y,
                       (page.rect.width + width) / 2, y + height)
    page.insert_image(target, stream=pix.tobytes('png'))
    return target.y1 + 8


def build(source_path, manifest_path, output_path, fontfile):
    started = time.perf_counter()
    source_path, manifest_path, output_path = map(Path, (source_path, manifest_path, output_path))
    fontfile = Path(fontfile)
    if not fontfile.is_file():
        raise FileNotFoundError(f'Chinese font not found: {fontfile}')
    if output_path.resolve() == source_path.resolve():
        raise ValueError('Output must not overwrite the source PDF')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    contract = validate_layout(manifest, 'appendix')
    font = fitz.Font(fontfile=str(fontfile))
    missing = sorted({ch for page in manifest.get('pages', []) for block in page.get('blocks', [])
                      for ch in block.get('text', '') + block.get('caption', '')
                      if not ch.isspace() and not font.has_glyph(ord(ch))})
    if missing:
        raise ValueError('Appendix font lacks glyphs: ' + ''.join(missing) +
                         '; choose a covering font or use the source builder with fonts.fallback.')
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if manifest.get('source_sha256') and manifest['source_sha256'] != source_hash:
        raise ValueError('Manifest source_sha256 does not match the supplied PDF')
    source = fitz.open(source_path)
    doc = fitz.open()
    toc = []
    page_reports = []
    source_to_output = {}

    for page_spec in manifest.get('pages', []):
        source_page = int(page_spec['source_page'])
        if not 1 <= source_page <= len(source):
            raise ValueError(f'Invalid source_page: {source_page}')
        if source_page in source_to_output:
            raise ValueError(f'Duplicate source_page: {source_page}')
        doc.insert_pdf(source, from_page=source_page - 1, to_page=source_page - 1)
        source_output_page = doc.page_count - 1
        source_to_output[source_page] = source_output_page
        toc.append([1, f'原文第{source_page}页', source_output_page + 1])

        translated_pages = []
        page = doc.new_page(width=source[source_page - 1].rect.width,
                            height=source[source_page - 1].rect.height)
        y = insert_header(page, source_page, fontfile)
        add_return_link(page, source_output_page, fontfile)
        block_count = 0

        def next_translation_page():
            nonlocal page, y
            translated_pages.append(page.number)
            page = doc.new_page(width=source[source_page - 1].rect.width,
                                height=source[source_page - 1].rect.height)
            y = insert_header(page, source_page, fontfile, continuation=True)
            add_return_link(page, source_output_page, fontfile)

        for block in page_spec.get('blocks', []):
            kind = block.get('type', 'paragraph')
            if kind in ('image', 'figure', 'equation'):
                crop = block.get('crop')
                if not crop:
                    raise ValueError(f'Media block needs crop coordinates: {block}')
                max_height = float(block.get('max_height', 180 if kind != 'equation' else 140))
                estimated = min(max_height, (crop['rect'][3] - crop['rect'][1]) * 2)
                if y + estimated + 12 > page.rect.height - 48:
                    next_translation_page()
                y = add_crop(page, source, crop, y, max_height=max_height)
                if block.get('caption'):
                    if y + 42 > page.rect.height - 48:
                        next_translation_page()
                    y = add_text(page, block['caption'], y, fontfile,
                                 size=float(block.get('caption_size', 9.5)))
            else:
                text = block.get('text', '').strip()
                if not text:
                    raise ValueError('Translation block has empty text')
                size = float(block.get('size', 15 if kind == 'heading' else 10.5))
                height = text_height(text, page.rect.width - 104, size,
                                     float(block.get('lineheight', 1.35 if kind == 'heading' else 1.45)))
                if y + height > page.rect.height - 48:
                    next_translation_page()
                if kind == 'heading':
                    y += 2
                y = add_text(page, text, y, fontfile, size=size,
                             lineheight=float(block.get('lineheight', 1.35 if kind == 'heading' else 1.45)))
            block_count += 1
        translated_pages.append(page.number)
        for n, out_page in enumerate(translated_pages):
            toc.append([1, f'第{source_page}页中文译文' + ('（续）' if n else ''), out_page + 1])
        page_reports.append({'source_page': source_page,
                             'source_output_page': source_output_page + 1,
                             'translation_output_pages': [n + 1 for n in translated_pages],
                             'translation_blocks': block_count})

    if not page_reports:
        raise ValueError('Manifest has no pages')
    doc.set_toc(toc)
    doc.set_metadata({'title': manifest.get('title', '中文论文阅读版'),
                      'subject': '原文页面完整保留，并按页附中文译文；图、公式按清单裁取原文。'})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    # Verify source pages survived byte-for-render unchanged and every translation page has text.
    saved = fitz.open(output_path)
    checks = []
    for item in page_reports:
        source_page = source[item['source_page'] - 1]
        output_page = saved[item['source_output_page'] - 1]
        identical = (source_page.get_pixmap(dpi=120).samples == output_page.get_pixmap(dpi=120).samples)
        if not identical:
            raise ValueError(f'Source page changed during append: {item["source_page"]}')
        for translation_page in item['translation_output_pages']:
            if not re.search(r'[\u3400-\u9fff]', saved[translation_page - 1].get_text()):
                raise ValueError(f'No searchable Chinese text on output page {translation_page}')
        checks.append({'source_page': item['source_page'], 'source_pixels_unchanged': True,
                       'translation_pages_have_searchable_chinese': True})
    links = []
    for page in saved:
        for link in page.get_links():
            if link.get('kind') == fitz.LINK_GOTO:
                if not 0 <= link.get('page', -1) < len(saved):
                    raise ValueError('Invalid internal PDF link destination')
                links.append({'source_page': page.number + 1, 'target_page': link['page'] + 1})
    report = {'output': str(output_path.resolve()), 'page_count': len(saved),
              'layout_contract': contract,
              'source_sha256': source_hash, 'pages': page_reports,
              'source_pixel_checks': checks, 'internal_goto_links': links,
              'translation_review': 'NOT AUTOMATED', 'reader_click_test': 'NOT PERFORMED',
              'build_seconds': round(time.perf_counter() - started, 3)}
    write_json(output_path.with_suffix('.validation.json'), report)
    saved.close()
    source.close()
    print(json.dumps(report, ensure_ascii=False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--manifest', required=True,
                        help='JSON translation manifest; schema documented in references/pdf-appendix-translation.md')
    parser.add_argument('--output', required=True)
    parser.add_argument('--font', required=True, help='Path to a Chinese-capable font file')
    args = parser.parse_args()
    build(args.source, args.manifest, args.output, args.font)


if __name__ == '__main__':
    main()
