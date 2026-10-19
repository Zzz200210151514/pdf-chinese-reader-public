"""Negative regressions for observed false passes; fixtures are synthetic PDFs."""
import hashlib,json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import pymupdf as fitz
from core.pipeline import finish, write
from core.preservation import audit_document
from core.validator import validate
from llm import Router


class ValidatorFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='v4-validator-regression-')
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.pdf'
        self.output = self.root / 'output.pdf'

    def tearDown(self):
        self.temporary.cleanup()

    def text_pdf(self, path, text):
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=400)
            page.insert_text((20, 50), text, fontname='china-s', fontsize=10)
            doc.save(path)

    def test_missing_latin_formula_cannot_pass_cjk_only_check(self):
        self.text_pdf(self.source, 'NaCl 10 mg')
        self.text_pdf(self.output, '盐 10 mg')
        manifest = {'pages': [{'source_page': 1, 'regions': [{'id': 'x', 'rect': [10, 10, 290, 90], 'html': '盐 NaCl 10 mg'}]}]}
        mapping = {'blocks': [{'id': 'x', 'source_text': 'NaCl 10 mg', 'translation': '盐 NaCl 10 mg', 'translation_required': True}]}
        result = validate(self.source, self.output, manifest, mapping)
        self.assertEqual(result['scientific_audit']['issues'], [])
        self.assertFalse(result['passed'])
        self.assertFalse(result['text_regions'][0]['searchable_and_copyable'])

    def test_missing_numeric_value_cannot_pass_cjk_only_check(self):
        self.text_pdf(self.source, 'NaCl 10 mg')
        self.text_pdf(self.output, '盐 NaCl mg')
        manifest = {'pages': [{'source_page': 1, 'regions': [{'id': 'x', 'rect': [10, 10, 290, 90], 'html': '盐 NaCl 10 mg'}]}]}
        mapping = {'blocks': [{'id': 'x', 'source_text': 'NaCl 10 mg', 'translation': '盐 NaCl 10 mg', 'translation_required': True}]}
        self.assertFalse(validate(self.source, self.output, manifest, mapping)['passed'])

    def linked_source(self):
        with fitz.open() as doc:
            doc.new_page(width=300, height=500)
            doc.new_page(width=300, height=500)
            doc[0].insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(20, 20, 80, 40), 'uri': 'https://example.org/paper'})
            doc.set_toc([[1, 'Mechanism', 2, {'kind': fitz.LINK_GOTO, 'page': 1, 'to': fitz.Point(30, 400), 'zoom': 2.0, 'bold': True, 'italic': True, 'color': (0.2, 0.3, 0.4)}]])
            doc.save(self.source)

    def test_same_uri_on_wrong_page_and_rect_is_missing_link(self):
        self.linked_source()
        with fitz.open(self.source) as doc:
            link = doc[0].get_links()[0]
            doc[0].delete_link(link)
            doc[1].insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(200, 200, 260, 220), 'uri': link['uri']})
            doc.save(self.output)
        with fitz.open(self.source) as original, fitz.open(self.output) as output:
            result = audit_document(original, output, [1, 2])
        self.assertEqual(result['missing_external_links'], {})
        self.assertEqual(result['missing_external_link_positions'], 1)
        self.assertFalse(result['passed'])

    def test_changed_bookmark_location_zoom_and_style_is_detected(self):
        self.linked_source()
        with fitz.open(self.source) as doc:
            # This is the exact former writer behavior: the simplified TOC discards details.
            doc.set_toc(doc.get_toc())
            doc.save(self.output)
        with fitz.open(self.source) as original, fitz.open(self.output) as output:
            result = audit_document(original, output, [1, 2])
            self.assertNotEqual(original.get_toc(False)[0][3]['to'], output.get_toc(False)[0][3]['to'])
        self.assertEqual(result['missing_bookmarks'], 1)
        self.assertFalse(result['passed'])

    def test_complex_bookmark_survives_actual_rebuild(self):
        self.linked_source()
        font = self.root / 'cjk.ttf'
        font.write_bytes(fitz.Font('cjk').buffer)
        manifest = {'fonts': {'regular': 'cjk.ttf'}, 'pages': [
            {'source_page': 1, 'regions': [{'id': 'translated', 'rect': [20, 70, 260, 110], 'html': '中文译文 NaCl 10 mg', 'size': 10, 'leading': 1.2}]},
            {'source_page': 2, 'regions': []},
        ]}
        path = self.root / 'layout.json'
        write(path, manifest)
        subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/pdf_reader.py'), 'build',
                        '--source', str(self.source), '--manifest', str(path), '--output', str(self.output),
                        '--font-dir', str(self.root)], check=True, capture_output=True, text=True, encoding='utf-8')
        with fitz.open(self.source) as original, fitz.open(self.output) as output:
            result = audit_document(original, output, [1, 2])
            source_details = original.get_toc(False)[0][3]
            output_details = output.get_toc(False)[0][3]
            for key in ('kind', 'page', 'to', 'zoom', 'bold', 'italic', 'color'):
                self.assertEqual(source_details.get(key), output_details.get(key), key)
        self.assertTrue(result['passed'], result)
        self.assertTrue(validate(self.source, self.output, manifest)['passed'])

    def test_explicit_pipeline_revision_takes_priority_over_prior_cache(self):
        work = self.root / 'work'
        mapping = {'layout_reviewed': True, 'pages':[{'source_page':1,'rect':[0,0,300,500]}],
                   'blocks': [{'id': 'x', 'source_page': 1, 'rect':[20,50,200,80], 'source_text': 'Water', 'translation_required': True}]}
        (self.root/'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)
        write(work / 'source_map.json', mapping)
        config = {'provider': 'host'}
        Router(config, cache_dir=work / 'cache').translate(mapping, lambda job: {'x': '初稿'})
        translations = work / 'revised.json'
        write(translations, {'x': '修订稿'})
        # Keep this regression focused on pipeline/router behavior; actual rebuild is tested above.
        self.text_pdf(self.output, '修订稿')
        rebuilt={'total_pages':1,'output_sha256':hashlib.sha256(self.output.read_bytes()).hexdigest()}
        with patch('core.pipeline.compile_layout', return_value={'pages': []}), patch('core.pipeline.rebuild', return_value=rebuilt), patch('core.pipeline.validate', return_value={'passed': True, 'scientific_audit': {'issues': []}}):
            finish(self.source, work, self.output, self.root, translations=translations, config=config, reviewed=True,fonts={'regular':'cjk.ttf'})
        merged = json.loads((work / 'merged.json').read_text(encoding='utf-8'))
        self.assertEqual(merged['blocks'][0]['translation'], '修订稿')

    def test_named_destination_native_outline_survives_actual_rebuild(self):
        # Construct a native outline whose /Dest points into a catalog /Names
        # destination tree. Serializing get_toc(False) via set_toc() previously
        # changed this LINK_NAMED bookmark into LINK_NONE.
        with fitz.open() as doc:
            doc.new_page(width=300, height=500)
            doc.set_toc([[1, 'Named mechanism', 1]])
            outline = doc.get_toc(False)[0][3]['xref']
            doc.xref_set_key(doc.pdf_catalog(), 'Names',
                             f'<< /Dests << /Names [(mechanism) [{doc[0].xref} 0 R /XYZ 30 100 2]] >> >>')
            doc.xref_set_key(outline, 'A', 'null')
            doc.xref_set_key(outline, 'Dest', '(mechanism)')
            doc.save(self.source)
        (self.root / 'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)
        manifest = {'fonts': {'regular': 'cjk.ttf'}, 'pages': [
            {'source_page': 1, 'regions': [{'id': 'translated', 'rect': [20, 70, 260, 110],
                                          'html': '中文译文 NaCl 10 mg', 'size': 10, 'leading': 1.2}]},
        ]}
        layout = self.root / 'named-layout.json'
        write(layout, manifest)
        subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/pdf_reader.py'), 'build',
                        '--source', str(self.source), '--manifest', str(layout), '--output', str(self.output),
                        '--font-dir', str(self.root)], check=True, capture_output=True, text=True, encoding='utf-8')
        with fitz.open(self.source) as original, fitz.open(self.output) as output:
            before = original.get_toc(False)
            after = output.get_toc(False)
            self.assertEqual(len(after), len(before))
            self.assertEqual(before[0][3]['kind'], fitz.LINK_NAMED)
            self.assertEqual(after[0][:3], before[0][:3])
            for key in ('kind', 'nameddest', 'page', 'to', 'zoom'):
                self.assertEqual(after[0][3].get(key), before[0][3].get(key), key)
            self.assertEqual(output.xref_get_key(after[0][3]['xref'], 'Dest'), ('string', 'mechanism'))
            self.assertTrue(audit_document(original, output, [1])['passed'])
        self.assertTrue(validate(self.source, self.output, manifest)['passed'])


if __name__ == '__main__':
    unittest.main()
