"""Fit-only preflight equivalence, shared fonts, and cross-phase logging."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
import pymupdf as fitz
import pdf_reader
from core.fonts import DEFAULT_FONTS,resolve_fonts
from core.runlog import RunLog
from core.validator import validate


class PerformanceTests(unittest.TestCase):
    def test_workflow_persists_but_invocation_is_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            first=RunLog(directory);second=RunLog(directory)
            self.assertEqual(first.workflow_id,second.workflow_id)
            self.assertNotEqual(first.run_id,second.run_id)
            with second.stage('layout',task_count=7):pass
            lines=[json.loads(s) for s in second.path.read_text('utf-8').splitlines()]
            self.assertEqual(lines[0]['stage_id'],lines[1]['stage_id'])
            self.assertEqual(lines[1]['task_count'],7)
            self.assertGreaterEqual(lines[1]['monotonic_seconds'],lines[0]['monotonic_seconds'])
            self.assertGreaterEqual(lines[1]['elapsed_seconds'],0)
            with self.assertRaises(ValueError):RunLog(directory,'another')

    def test_usage_null_and_nested_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            event=RunLog(directory).event('translate','completed',
                input_tokens=42,output_tokens=None,usage={'cached_tokens':None,'api_key':'hidden'},
                authorization='hidden',nested=[{'access_token':'hidden','model':'test'}])
            self.assertEqual(event['input_tokens'],42)
            self.assertIsNone(event['output_tokens'])
            self.assertEqual(event['usage'],{'cached_tokens':None})
            self.assertNotIn('hidden',json.dumps(event))

    def test_failed_stage_is_linked_and_retains_context(self):
        with tempfile.TemporaryDirectory() as directory:
            log=RunLog(directory)
            with self.assertRaises(RuntimeError):
                with log.stage('rebuild',attempt=2):raise RuntimeError('secret-like exception not recorded')
            lines=[json.loads(s) for s in log.path.read_text('utf-8').splitlines()]
            self.assertEqual(lines[1]['status'],'failed')
            self.assertEqual(lines[1]['attempt'],2)
            self.assertEqual(lines[1]['error_type'],'RuntimeError')
            self.assertNotIn('secret-like',log.path.read_text())

    def test_defaults_override_alias_and_missing_file(self):
        self.assertEqual(resolve_fonts(),DEFAULT_FONTS)
        self.assertEqual(resolve_fonts({'regular':'custom.ttf'}),{'regular':'custom.ttf','bold':'custom.ttf'})
        self.assertEqual(resolve_fonts({'regular':'a','symbol':'b'})['fallback'],'b')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):resolve_fonts(font_dir=directory)

    def test_fit_only_matches_insert_htmlbox(self):
        # Use a portable Latin font, ensuring CI does not depend on Windows fonts.
        with tempfile.TemporaryDirectory() as directory:
            fontdir=Path(directory)
            (fontdir/'regular.ttf').write_bytes(fitz.Font('helv').buffer)
            m={'fonts':{'regular':'regular.ttf'}}
            css,archive=pdf_reader.style(m,fontdir)
            fonts=pdf_reader.FontCoverage(m,fontdir)
            cases=[
                {'rect':[10,10,270,180],'html':'<b>Heading</b><br/>Some H<sub>2</sub>O and 1.25 mg/L. <a href="https://example.org">Link</a>'},
                {'rect':[10,10,55,28],'html':'Too many scientific words to fit in a tiny area','min_scale':1},
                {'rect':[10,10,100,70],'html':'Repeated words '*15,'min_scale':0.3},
            ]
            for item in cases:
                bounds=fitz.Rect(0,0,300,200)
                actual=pdf_reader.fit_only(item,bounds,fonts,css,archive)
                with fitz.open() as doc:
                    page=doc.new_page(width=300,height=200)
                    expected=page.insert_htmlbox(item['rect'],pdf_reader.content(item,fonts),css=css,
                        archive=archive,scale_low=item.get('min_scale',1))
                self.assertEqual(actual,expected)

    def test_display_list_reuse_matches_clipped_page_pixels(self):
        with fitz.open() as doc:
            page=doc.new_page(width=300,height=200)
            page.insert_text((20,40),'Native content',fontsize=13)
            page.draw_rect((40,80,170,150),fill=(0.1,0.3,0.8))
            page.add_rect_annot((20,30,160,45))
            cache={}
            for rectangle in ([10.2,20.4,170.7,70.1],[40,80,170,150],[0,0,300,200]):
                expected=page.get_pixmap(dpi=120,clip=fitz.Rect(rectangle))
                actual=pdf_reader.protected_pixmap(doc,0,fitz.Rect(rectangle),cache)
                self.assertEqual((actual.width,actual.height,actual.samples),
                    (expected.width,expected.height,expected.samples))
            self.assertEqual(len(cache),1)

    def test_cached_protected_checks_still_reject_native_damage(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.pdf';output=Path(directory)/'output.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=300,height=200)
                page.draw_rect((40,80,170,150),fill=(0.1,0.3,0.8))
                doc.save(source)
            manifest={'pages':[{'source_page':1,'protected':[{'id':'native','rect':[40,80,170,150]}]}]}
            output.write_bytes(source.read_bytes())
            self.assertTrue(validate(source,output,manifest)['passed'])
            with fitz.open(source) as doc:
                doc[0].draw_rect((45,85,60,100),fill=(1,1,1))
                doc.save(output)
            report=validate(source,output,manifest)
            self.assertFalse(report['protected'][0]['pixels_unchanged'])
            self.assertFalse(report['passed'])

    def test_protected_graphic_rendering_delta_is_warning(self):
        # A sub-1% delta on a protected figure (anti-aliasing, compression,
        # sub-pixel shift) is reported as a warning, not treated as damage.
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.pdf';output=Path(directory)/'output.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=300,height=200)
                page.draw_rect((40,80,170,150),fill=(0.1,0.3,0.8))
                doc.save(source)
            with fitz.open(source) as doc:
                doc[0].draw_rect((45,85,45.7,85.7),fill=(1,1,1))
                doc.save(output)
            manifest={'pages':[{'source_page':1,'protected':[{'id':'fig','type':'figure','rect':[40,80,170,150]}]}]}
            report=validate(source,output,manifest)
            item=report['protected'][0]
            self.assertFalse(item['pixels_unchanged'])
            self.assertTrue(item['pixel_warning'])
            self.assertEqual(item['pixel_level'],'warning')
            self.assertLessEqual(item['pixel_diff_percent'],1.0)
            self.assertTrue(report['passed'])

    def test_protected_graphic_visible_damage_still_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.pdf';output=Path(directory)/'output.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=300,height=200)
                page.draw_rect((40,80,170,150),fill=(0.1,0.3,0.8))
                doc.save(source)
            with fitz.open(source) as doc:
                doc[0].draw_rect((45,85,60,100),fill=(1,1,1))
                doc.save(output)
            manifest={'pages':[{'source_page':1,'protected':[{'id':'fig','type':'figure','rect':[40,80,170,150]}]}]}
            report=validate(source,output,manifest)
            item=report['protected'][0]
            self.assertEqual(item['pixel_level'],'blocking')
            self.assertGreater(item['pixel_diff_percent'],1.0)
            self.assertFalse(report['passed'])


if __name__=='__main__':unittest.main()
