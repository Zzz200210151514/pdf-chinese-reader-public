"""Reading copies accept bounded fitting changes without hiding content loss."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import pymupdf as fitz
from pdf_reader import build, check_layout
from core.doctor import status


class RelaxedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)
        self.source = self.root / 'source.pdf'
        with fitz.open() as doc:
            doc.new_page(width=300, height=400)
            doc.save(self.source)
        self.manifest = {'fonts': {'regular': 'cjk.ttf', 'bold': 'cjk.ttf'},
                         'pages': [{'source_page': 1, 'regions': [{
                             'id': 'micro', 'rect': [30, 40, 58, 50],
                             'html': '[12–14]。', 'size': 8, 'leading': 1.17,
                             'min_scale': .88, 'inline_fragment': True,
                             'block_type': 'paragraph', 'source_block_ids': ['micro']}]}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_micro_overflow_auto_fits_and_delivers_complete_text(self):
        report = check_layout(self.source, self.manifest, self.root)
        self.assertTrue(report['layout_checks_passed'], report['issues'])
        self.assertTrue(report.get('warnings'), 'Automatic relaxation must be reported')
        path = self.root / 'layout.json'
        path.write_text(json.dumps(self.manifest), encoding='utf-8')
        output = self.root / 'reading.pdf'
        build(SimpleNamespace(source=self.source, manifest=path, output=output, font_dir=self.root))
        with fitz.open(output) as doc:
            self.assertEqual(''.join(doc[0].get_text().split()), '[12–14]。')
        final = json.loads(output.with_suffix('.validation.json').read_text(encoding='utf-8'))
        self.assertEqual(final.get('delivery_status'), 'passed_with_warnings')
        self.assertGreater(final.get('warning_count', 0), 0)
        self.assertTrue(output.with_suffix('.overview.pdf').is_file())

    def test_title_is_not_treated_as_micro_fragment(self):
        m = copy.deepcopy(self.manifest)
        m['pages'][0]['regions'][0]['block_type'] = 'title'
        report = check_layout(self.source, m, self.root)
        self.assertTrue(any(i['check'] == 'overflow' for i in report['issues']))
        self.assertEqual(m['pages'][0]['regions'][0]['min_scale'], .88)

    def test_long_prose_does_not_receive_micro_shrink(self):
        m = copy.deepcopy(self.manifest)
        m['pages'][0]['regions'][0]['html'] = 'Complete methods and experimental conditions. ' * 30
        report = check_layout(self.source, m, self.root)
        self.assertTrue(any(i['check'] == 'overflow' for i in report['issues']))
        self.assertEqual(m['pages'][0]['regions'][0]['min_scale'], .88)

    def test_missing_glyph_still_blocks(self):
        m = copy.deepcopy(self.manifest)
        m['pages'][0]['regions'][0]['html'] = '\U0010ffff'
        report = check_layout(self.source, m, self.root)
        self.assertTrue(any(i['check'] == 'missing-glyph' for i in report['issues']))
        self.assertFalse(report['layout_checks_passed'])

    def test_unrenderable_micro_block_still_blocks(self):
        self.manifest['pages'][0]['regions'][0]['rect'][2] = 35
        report = check_layout(self.source, self.manifest, self.root)
        self.assertTrue(any(i['check'] == 'overflow' for i in report['issues']))

    def test_rechecking_effective_layout_retains_warning(self):
        first = check_layout(self.source, self.manifest, self.root)
        second = check_layout(self.source, self.manifest, self.root)
        self.assertTrue(first.get('warnings'))
        self.assertEqual(first['warnings'], second['warnings'])

    def test_intermediate_merge_after_failed_build_is_not_delivery(self):
        for name in ('source_map.json', 'frozen-map.json', 'translations.json', 'merged.json'):
            (self.root / name).write_text('{}', encoding='utf-8')
        (self.root / 'events.jsonl').write_text(json.dumps({'stage': 'rebuild', 'status': 'failed'}) + '\n')
        self.assertEqual(status(self.root)['stage'], 'finish')

    def test_completed_delivery_ignores_stale_pending_jobs(self):
        for name in ('source_map.json', 'frozen-map.json', 'translations.json'):
            (self.root / name).write_text('{}', encoding='utf-8')
        (self.root / 'pending-jobs.json').write_text('[{"id":"stale"}]', encoding='utf-8')
        receipt={'status':'passed_with_warnings','output':str(self.source),
                 'output_sha256':hashlib.sha256(self.source.read_bytes()).hexdigest(),'warning_count':1}
        (self.root / 'delivery.json').write_text(json.dumps(receipt), encoding='utf-8')
        self.assertEqual(status(self.root)['stage'], 'verify')
        self.assertTrue(status(self.root)['delivery']['output_verified'])
        self.source.write_bytes(b'changed')
        self.assertFalse(status(self.root)['delivery']['output_verified'])


if __name__ == '__main__':
    unittest.main()
