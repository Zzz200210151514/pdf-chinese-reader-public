"""MinerU and local extraction share one in-place source map."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymupdf as fitz

from core.classifier import classify
from core.mineru_hybrid import fuse_mineru
from core.pdf_reader import parse
from core.pipeline import prepare
from core.structure import source_order


def _pdf(path, lines, size=(400, 500)):
    with fitz.open() as doc:
        page = doc.new_page(width=size[0], height=size[1])
        for text, point in lines:
            page.insert_text(point, text, fontsize=11)
        doc.save(path)


def _norm(rect, bounds):
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    pad = 2
    box = [rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad]
    return [max(0, box[0] / width * 1000), max(0, box[1] / height * 1000),
            min(1000, box[2] / width * 1000), min(1000, box[3] / height * 1000)]


class HybridExtractionTests(unittest.TestCase):
    def test_native_spans_keep_geometry_and_follow_mineru_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'paper.pdf'
            _pdf(source, [
                ('HIGHLIGHTS', (40, 70)),
                ('Alpha sentence is complete.', (40, 120)),
                ('Beta sentence is complete.', (40, 180)),
            ])
            mapping = classify(parse(source))
            by_text = {b['source_text']: b for b in mapping['blocks']}
            before = {b['id']: list(b['rect']) for b in mapping['blocks']}
            bounds = mapping['pages'][0]['rect']
            beta = by_text['Beta sentence is complete.']
            alpha = by_text['Alpha sentence is complete.']
            heading = by_text['HIGHLIGHTS']
            items = [
                {'type': 'text', 'text': 'Beta sentence is complete.', 'text_level': 0,
                 'page_idx': 0, 'bbox': _norm(beta['rect'], bounds)},
                {'type': 'text', 'text': 'HIGHLIGHTS', 'text_level': 2,
                 'page_idx': 0, 'bbox': _norm(heading['rect'], bounds)},
                {'type': 'text', 'text': 'Alpha sentence is complete.',
                 'page_idx': 0, 'bbox': _norm(alpha['rect'], bounds)},
            ]
            # text_level 0 is falsy; the heading item carries the level.
            items[0].pop('text_level')
            extraction = root / 'extraction'
            extraction.mkdir()
            (extraction / 'content_list.json').write_text(json.dumps(items), encoding='utf-8')
            report = fuse_mineru(mapping, extraction)
            self.assertTrue(report['applied'])
            self.assertEqual(before[beta['id']], list(beta['rect']))
            self.assertEqual(before[alpha['id']], list(alpha['rect']))
            self.assertEqual(heading['type'], 'heading')
            self.assertTrue(heading['translation_required'])
            self.assertEqual(beta['source_text'], 'Beta sentence is complete.')
            ordered = source_order(mapping)
            self.assertLess(ordered.index(beta['id']), ordered.index(heading['id']))
            self.assertLess(ordered.index(heading['id']), ordered.index(alpha['id']))

    def test_split_native_lines_become_one_paragraph_without_new_rects(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'paper.pdf'
            _pdf(source, [
                ('The measured rejection was', (40, 100)),
                ('higher than the control.', (40, 200)),
            ])
            mapping = classify(parse(source))
            prose = [b for b in mapping['blocks'] if b.get('translation_required')]
            self.assertGreaterEqual(len(prose), 2)
            bounds = mapping['pages'][0]['rect']
            union = [min(b['rect'][0] for b in prose), min(b['rect'][1] for b in prose),
                     max(b['rect'][2] for b in prose), max(b['rect'][3] for b in prose)]
            rects = {b['id']: list(b['rect']) for b in prose}
            fonts = {b['id']: b.get('source_font_pt') for b in prose}
            extraction = root / 'extraction'
            extraction.mkdir()
            (extraction / 'content_list.json').write_text(json.dumps([{
                'type': 'text', 'page_idx': 0, 'bbox': _norm(union, bounds),
                'text': 'The measured rejection was higher than the control.',
            }]), encoding='utf-8')
            fuse_mineru(mapping, extraction)
            linked = [b for b in mapping['blocks'] if b['id'] in rects and b.get('logical_paragraph_id')]
            self.assertGreaterEqual(len(linked), 2)
            anchor = next(b for b in linked if not b.get('translation_in'))
            self.assertIn('higher than the control', anchor['logical_source'])
            for block in linked:
                self.assertEqual(rects[block['id']], list(block['rect']))
                self.assertEqual(fonts[block['id']], block.get('source_font_pt'))

    def test_scanned_page_uses_mineru_ocr_text_and_box(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'scan.pdf'
            with fitz.open() as doc:
                doc.new_page(width=300, height=400)
                doc.save(source)
            mapping = classify(parse(source))
            self.assertFalse(mapping['blocks'])
            extraction = root / 'extraction'
            extraction.mkdir()
            (extraction / 'content_list.json').write_text(json.dumps([{
                'type': 'text', 'text': 'Scanned flux remained stable.',
                'bbox': [100, 200, 800, 320], 'page_idx': 0,
            }]), encoding='utf-8')
            report = fuse_mineru(mapping, extraction)
            self.assertEqual(report['ocr_blocks'], 1)
            block = mapping['blocks'][0]
            self.assertEqual(block['source_text'], 'Scanned flux remained stable.')
            self.assertEqual(block['origin'], 'mineru-ocr')
            self.assertTrue(block['translation_required'])
            self.assertNotIn('no-extractable-text', [i.get('check') for i in mapping['issues']])
            page = mapping['pages'][0]['rect']
            self.assertGreaterEqual(block['rect'][0], page[0])
            self.assertLessEqual(block['rect'][2], page[2])

    def test_equation_region_stays_native(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'eq.pdf'
            _pdf(source, [('Water uptake equals the displayed formula.', (40, 80)),
                          ('H2O + CO2', (40, 140))])
            mapping = classify(parse(source))
            formula = next(b for b in mapping['blocks'] if 'H2O' in b['source_text'])
            rect = list(formula['rect'])
            bounds = mapping['pages'][0]['rect']
            extraction = root / 'extraction'
            extraction.mkdir()
            (extraction / 'content_list.json').write_text(json.dumps([{
                'type': 'equation', 'text': 'H2O + CO2', 'page_idx': 0,
                'bbox': _norm(formula['rect'], bounds),
            }]), encoding='utf-8')
            fuse_mineru(mapping, extraction)
            self.assertFalse(formula['translation_required'])
            self.assertEqual(formula['preservation_reason'], 'mineru-formula')
            self.assertEqual(rect, list(formula['rect']))
            self.assertIn('H2O', formula['source_text'])


class ExtractorModeTests(unittest.TestCase):
    def _source(self, root):
        path = root / 'paper.pdf'
        _pdf(path, [('A readable native paragraph for extraction.', (40, 80))])
        return path

    def test_auto_without_token_stays_local(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {k: v for k, v in os.environ.items() if k not in {'MINERU_API_KEY', 'MINERU_TOKEN', 'MINERU_TOKEN_FILE'}}
            with patch.dict(os.environ, env, clear=True), \
                    patch('core.pipeline.mineru_configured', return_value=False), \
                    patch('core.pipeline.extract_with_mineru', side_effect=AssertionError('MinerU must not run')):
                mapping, jobs = prepare(self._source(root), root / 'work', extractor='auto')
            self.assertEqual(mapping['extraction']['requested'], 'auto')
            self.assertEqual(mapping['extraction']['extractor'], 'local')
            self.assertTrue(any(item['check'] == 'mineru-not-configured' for item in mapping['extraction']['warnings']))
            self.assertFalse(jobs)

    def test_auto_falls_back_when_mineru_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('core.pipeline.mineru_configured', return_value=True), \
                    patch('core.pipeline.extract_with_mineru', return_value=(False, 'service unavailable')):
                mapping, _jobs = prepare(self._source(root), root / 'work', extractor='auto')
            self.assertEqual(mapping['extraction']['extractor'], 'local')
            self.assertTrue(any(item['check'] == 'mineru-fallback' for item in mapping['extraction']['warnings']))

    def test_explicit_mineru_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('core.pipeline.extract_with_mineru', return_value=(False, 'service unavailable')):
                with self.assertRaisesRegex(ValueError, 'service unavailable'):
                    prepare(self._source(root), root / 'work', extractor='mineru')

    def test_staged_content_list_skips_the_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            mapping = classify(parse(source))
            block = next(b for b in mapping['blocks'] if b.get('translation_required'))
            bounds = mapping['pages'][0]['rect']
            extraction = root / 'work' / 'extraction'
            extraction.mkdir(parents=True)
            (extraction / 'content_list.json').write_text(json.dumps([{
                'type': 'text', 'text': block['source_text'], 'text_level': 2,
                'page_idx': 0, 'bbox': _norm(block['rect'], bounds),
            }]), encoding='utf-8')
            with patch('core.pipeline.extract_with_mineru', side_effect=AssertionError('staged list must be reused')):
                prepared, _jobs = prepare(source, root / 'work', extractor='mineru', reuse_extraction=True)
            body = next(b for b in prepared['blocks'] if b['source_text'] == block['source_text'])
            self.assertEqual(body['type'], 'heading')
            self.assertEqual(prepared['extraction']['extractor'], 'mineru')
            self.assertEqual(list(body['rect']), list(block['rect']))


if __name__ == '__main__':
    unittest.main()
