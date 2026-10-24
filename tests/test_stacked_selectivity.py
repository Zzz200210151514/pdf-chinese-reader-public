"""ACS stacked selectivity symbols, script-fragment merges, PUA digits, 9 pt floor."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'scripts'))

from core.classifier import BODY_FONT_FLOOR, body_font_floor_applies, classify
from core.layout import compile_layout
from core.paragraphs import link_paragraphs
from core.pdf_reader import records_from_pages
from core.stacked_selectivity import apply_pua_digits, is_stacked_script_fragment, synthesize_stacked_selectivity
from core.structure import preflight
from pdf_reader import check_layout, micro_region
from pdf_fonts import canonicalize_digit_cmaps
from test_layout_consistency_2026_10 import _prose
from test_parser_layout import block, inspection, line, merged


def _span(text, box, size, origin_y=None, flags=0):
    return {'text': text, 'bbox': list(box), 'size': size, 'font': 'Times-Italic', 'flags': flags,
            'origin': [box[0], box[3] - 1 if origin_y is None else origin_y]}


def _text_block(spans_or_lines, box=None):
    """One PyMuPDF block. A list of spans is one line; a list of lists is lines."""
    if spans_or_lines and isinstance(spans_or_lines[0], list):
        lines = []
        for spans in spans_or_lines:
            boxes = [span['bbox'] for span in spans]
            lines.append({'bbox': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                   max(b[2] for b in boxes), max(b[3] for b in boxes)],
                          'dir': [1, 0], 'wmode': 0, 'spans': spans})
    else:
        spans = spans_or_lines
        boxes = [span['bbox'] for span in spans]
        lines = [{'bbox': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                           max(b[2] for b in boxes), max(b[3] for b in boxes)],
                  'dir': [1, 0], 'wmode': 0, 'spans': spans}]
    boxes = [item['bbox'] for item in lines]
    return {'type': 0,
            'bbox': box or [min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes)],
            'lines': lines}


class StackedSelectivityTests(unittest.TestCase):
    def _page(self):
        # Two raised pluses over S, with Na above and Ca/2 below, plus the
        # chloride/sulfate twin and the word "and" sitting between them.
        prose_above = _span('The selectivities were measured after each run.', [40, 40, 320, 54], 10, 52)
        plus_na = _span('+', [46, 62, 52, 70], 5, 69)
        plus_ca = _span('+', [52, 70, 58, 78], 5, 77)
        base = _span('S', [40, 80, 48, 92], 10, 90)
        calcium = _span('Ca', [48, 88, 60, 98], 6, 96)
        two = _span('2', [60, 90, 66, 98], 5, 97)
        conjunction = _span('and', [90, 80, 112, 92], 10, 90)
        base_cl = _span('S', [120, 80, 128, 92], 10, 90)
        sulfate = _span('SO', [126, 88, 140, 98], 6, 96)
        four = _span('4', [140, 92, 146, 100], 5, 99)
        prose_below = _span('are determined by the flux ratio.', [40, 110, 280, 124], 10, 122)
        sodium = _span('Na', [50, 72, 62, 82], 6, 81)
        chloride = _span('Cl', [126, 70, 138, 80], 6, 79)
        body = _text_block([
            [prose_above],
            [plus_na, plus_ca, base, calcium, two, conjunction, base_cl, sulfate, four],
            [prose_below],
        ])
        return inspection([
            body,
            _text_block([sodium]),
            _text_block([chloride]),
        ])

    def test_stacked_charges_become_one_math_block_per_symbol(self):
        data = self._page()
        out = classify(data)
        formulas = [item for item in out['blocks']
                    if item.get('preservation_reason') == 'native-stacked-selectivity']
        self.assertEqual(len(formulas), 2)
        for formula in formulas:
            self.assertFalse(formula['translation_required'])
            self.assertEqual(formula['type'], 'formula')
            self.assertIn('<sup>', formula['source_text'])
            self.assertIn('<sub>', formula['source_text'])
            self.assertIn('S', formula['source_text'])
            self.assertNotRegex(formula['source_text'], r'^S[A-Za-z0-9+\-]+$')
        cation = next(item for item in formulas if 'Ca' in item['source_text'])
        anion = next(item for item in formulas if 'Cl' in item['source_text'] or 'SO' in item['source_text'])
        self.assertIn('+', cation['source_text'])
        self.assertIn('Na', cation['source_text'])
        self.assertTrue('Cl' in anion['source_text'] or 'SO' in anion['source_text'])
        prose = [item for item in out['blocks'] if item.get('translation_required')]
        prose_text = '\n'.join(item['source_text'] for item in prose)
        self.assertNotRegex(prose_text, r'(^|\n)\+\s*(\n|$)')
        self.assertIn('selectivities', prose_text)
        self.assertIn('determined', prose_text)
        self.assertIn('and', prose_text)
        for formula in formulas:
            for item in prose:
                self.assertFalse(_overlaps(item['rect'], formula['rect']),
                                 (item['source_text'], item['rect'], formula['rect']))
        records, _, _ = records_from_pages(data['pages'])
        data['records'] = records
        gate = preflight(out, data)
        blocking = [issue for issue in gate['issues']
                    if issue.get('check') in {'source-text-coverage', 'protected-intersection'}
                    and issue.get('severity') == 'blocking']
        self.assertEqual(blocking, [], gate['issues'])
        layout = compile_layout(merged_without_reclassify(out))
        protected = {item['id'] for item in layout['pages'][0]['protected']}
        self.assertTrue(all(formula['id'] in protected for formula in formulas))
        rendered = '\n'.join(region['html'] for region in layout['pages'][0]['regions'])
        self.assertNotRegex(rendered, r'(^|\n|>)\+(<|\n|$)')
        for region in layout['pages'][0]['regions']:
            if region.get('block_type') in {'paragraph', 'abstract'}:
                self.assertGreaterEqual(float(region.get('size', 0)), BODY_FONT_FLOOR)

    def test_lower_charge_and_nearby_twin_stay_in_their_own_formulas(self):
        # Actual ACS geometry: the calcium charge is 1 pt below S's baseline,
        # and the anion twin is only 36 pt to its right.
        spans = [
            _span('+', [422.5, 538.4, 425.9, 543.4], 5.01, 542.23),
            _span('+', [419.8, 531.7, 423.3, 536.7], 5.01, 535.51),
            _span('S', [408.6, 533.5, 412.8, 542.5], 9, 541.16),
            _span('Ca', [412.8, 539.6, 419.5, 545.9], 6.3, 545.12),
            _span('Na', [414, 532.9, 421.1, 539.2], 6.3, 538.39),
            _span('2', [419.8, 537.9, 422.2, 542.9], 5.01, 542.23),
            _span('and', [429.4, 535.1, 442.1, 544.1], 9, 541.19),
            _span(' S', [442.1, 533.5, 449.1, 542.5], 9, 541.19),
            _span('Cl', [450.1, 533, 455.7, 539.3], 6.3, 538.44),
            _span('SO', [449.2, 540.3, 456.9, 546.6], 6.3, 545.76),
            _span('4', [456.8, 543, 459.2, 548], 5.01, 547.32),
            _span('2', [456.8, 538.5, 459.2, 543.5], 5.01, 542.88),
            # Nearby body digits and ions in another line are not scripts.
            _span('35', [450, 511, 460, 520], 9, 518),
            _span('Na', [452, 510, 460, 517], 6.3, 516),
        ]
        blocks = [_text_block(spans)]
        synthesize_stacked_selectivity(blocks)
        formulas = [b for b in blocks if b.get('stacked_selectivity')]
        self.assertEqual(len(formulas), 2)
        cation = next(b for b in formulas if 'Ca' in b['stacked_markup'])
        anion = next(b for b in formulas if 'Cl' in b['stacked_markup'])
        self.assertEqual(cation['stacked_markup'].count('+'), 2)
        self.assertNotIn('Cl', cation['stacked_markup'])
        self.assertNotIn('35', anion['stacked_markup'])
        self.assertNotIn('Na', anion['stacked_markup'])
        self.assertLess(cation['bbox'][2], 426)
        self.assertGreater(anion['bbox'][0], 440)
        remainder = ''.join(sp['text'] for b in blocks if not b.get('stacked_selectivity')
                            for line in b['lines'] for sp in line['spans'])
        self.assertNotIn('+', remainder)
        self.assertIn('35', remainder)

    def test_short_chinese_body_fragments_keep_floor(self):
        for text in ('和 S<sub>SO</sub>', '，在无结垢', 'and', 'without the formation of'):
            region = {'block_type': 'paragraph', 'html': text,
                      'rect': [40, 40, 70, 55], 'inline_fragment': True,
                      'source_block_ids': ['fragment']}
            self.assertTrue(body_font_floor_applies(region), text)
            self.assertFalse(micro_region(region), text)
        self.assertFalse(body_font_floor_applies({
            'block_type': 'paragraph', 'html': '[12]', 'rect': [40, 40, 60, 50]}))

    def test_noto_instrument_digits_roundtrip_as_ascii(self):
        font = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
        if not font.exists():
            self.skipTest('Noto CJK not installed')
        with tempfile.TemporaryDirectory() as temp:
            with fitz.open() as doc:
                page = doc.new_page()
                first = doc.xref_length()
                page.insert_htmlbox(fitz.Rect(20, 20, 350, 100), 'CHI 660E 数字 0123456789',
                    css="@font-face{font-family:test;src:url(" + font.name + ")}body{font-family:test}",
                    archive=fitz.Archive(str(font.parent)))
                before = page.get_pixmap().samples
                canonicalize_digit_cmaps(doc, first)
                target = Path(temp) / 'digits.pdf'
                doc.save(target)
            with fitz.open(target) as doc:
                text = doc[0].get_text().replace('\u00a0', ' ')
                self.assertIn('CHI 660E', text)
                self.assertIn('0123456789', text)
                self.assertNotRegex(text, r'[\ue000-\uf8ff]')
                self.assertEqual(before, doc[0].get_pixmap().samples)

    def test_synthesis_is_idempotent(self):
        data = self._page()
        synthesize_stacked_selectivity(data['pages'][0]['blocks'])
        first = sum(1 for block in data['pages'][0]['blocks'] if block.get('stacked_selectivity'))
        synthesize_stacked_selectivity(data['pages'][0]['blocks'])
        second = sum(1 for block in data['pages'][0]['blocks'] if block.get('stacked_selectivity'))
        self.assertEqual(first, 2)
        self.assertEqual(second, 2)

    def test_script_fragment_does_not_merge_into_prose(self):
        data = inspection([
            block('The measured water flux was still rising.', [40, 100, 280, 140], 10),
            block('+', [150, 108, 158, 116], 5),
            block('Cl', [160, 104, 172, 114], 6),
            block('S<sub>Ca</sub>', [180, 100, 210, 118], 8),
        ])
        # The angle-bracket block is one span, so it is not a geometric stack.
        # Layout must still refuse to fold these fragments into the sentence.
        out = merged(data)
        layout = compile_layout(out)
        regions = layout['pages'][0]['regions']
        prose = next(region for region in regions if 'water flux' in region['html'])
        self.assertNotIn('+', prose['html'])
        self.assertNotIn('Cl', prose['html'])
        self.assertNotIn('S<sub>Ca</sub>', prose['html'])
        self.assertGreater(len(regions), 1)
        self.assertGreaterEqual(float(prose['size']), BODY_FONT_FLOOR)

    def test_paragraph_linker_skips_charge_lines(self):
        blocks = [
            _prose('a', 'The measured water flux was', [40, 100, 280, 130]),
            _prose('b', '+', [40, 132, 48, 140], size=5),
            _prose('c', 'higher than the control value.', [40, 142, 280, 170]),
        ]
        mapping = {'blocks': blocks, 'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800]}],
                   'expected_block_ids': [item['id'] for item in blocks]}
        link_paragraphs(mapping)
        self.assertNotIn('translation_in', blocks[1])
        self.assertNotIn('logical_paragraph_id', blocks[1])
        self.assertTrue(is_stacked_script_fragment('+'))
        self.assertTrue(is_stacked_script_fragment('Cl'))
        self.assertTrue(is_stacked_script_fragment('4\n<sub>2</sub>'))
        self.assertFalse(is_stacked_script_fragment('The measured water flux was'))
        self.assertFalse(is_stacked_script_fragment('2'))

    def test_pua_instrument_digits_become_ascii(self):
        raw = 'CHI \uf6b7\uf6b7\uf6b1E'
        span = _span(raw, [40, 40, 120, 52], 10, 50)
        apply_pua_digits([_text_block([span])])
        self.assertEqual(span['text'], 'CHI 660E')
        data = inspection([block('The workstation (CHI \uf6b7\uf6b7\uf6b1E) recorded the voltage.',
                                 [40, 80, 360, 110], 10)])
        out = classify(data)
        body = next(item for item in out['blocks'] if item.get('translation_required'))
        self.assertIn('CHI 660E', body['source_text'])
        self.assertNotIn('\uf6b7', body['source_text'])
        self.assertNotIn('\uf6b8', body['source_text'])
        self.assertNotIn('\uf6b1', body['source_text'])

    def test_body_region_cannot_shrink_below_nine_points(self):
        data = merged(inspection([
            block('The membrane flux stayed stable across the whole experiment and the next line.',
                  [40, 100, 400, 180], 5),
        ]))
        layout = compile_layout(data)
        region = layout['pages'][0]['regions'][0]
        self.assertGreaterEqual(float(region['size']), BODY_FONT_FLOOR)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)
            source = root / 'source.pdf'
            with fitz.open() as doc:
                doc.new_page(width=500, height=400)
                doc.save(source)
            manifest = {'fonts': {'regular': 'cjk.ttf', 'bold': 'cjk.ttf'},
                        'pages': [{'source_page': 1, 'regions': [{
                            'id': 'body', 'rect': [40, 40, 360, 160], 'block_type': 'paragraph',
                            'html': '膜通量在这一栏中保持可读，不应被压到五磅。',
                            'size': 5, 'leading': 1.17, 'min_scale': 0.5, 'source_font_pt': 5,
                            'source_block_ids': ['body']}]}]}
            report = check_layout(source, manifest, root)
            self.assertTrue(report['layout_checks_passed'], report['issues'])
            fitted = manifest['pages'][0]['regions'][0]
            self.assertGreaterEqual(float(fitted['size']), BODY_FONT_FLOOR)
            self.assertGreaterEqual(float(fitted['size']) * float(fitted.get('min_scale', 1)),
                                    BODY_FONT_FLOOR - 0.05)


def merged_without_reclassify(mapping):
    mapping = dict(mapping)
    mapping['layout_reviewed'] = True
    for item in mapping['blocks']:
        if item.get('translation_required') and not item.get('translation'):
            item['translation'] = '完整译文 ' + re.sub(r'<[^>]+>', '', item.get('source_text', ''))
    return mapping


def _overlaps(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


if __name__ == '__main__':
    unittest.main()
