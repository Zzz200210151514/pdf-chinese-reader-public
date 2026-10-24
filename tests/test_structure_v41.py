import sys,unittest,tempfile
from pathlib import Path
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.structure import preflight,source_order,audit_order,fingerprint
from core.pipeline import write,plan,translate,prepare
from core.layout import compile_layout,LayoutError

def mapping():
    blocks=[{'id':'r','source_page':1,'rect':[320,80,550,110],'source_text':'Right paragraph.', 'translation':'右段'},
            {'id':'heading','source_page':1,'rect':[30,100,260,120],'source_text':'2.1 Methods','translation':'2.1 方法'},
            {'id':'body','source_page':1,'rect':[30,125,260,180],'source_text':'Left paragraph.','translation':'左段'}]
    return {'source_sha256':'a'*64,'layout_reviewed':True,'pages':[{'source_page':1,'rect':[0,0,600,800],'protected':[]}],
            'blocks':blocks,'expected_block_ids':[b['id'] for b in blocks],'issues':[]}

class StructureTests(unittest.TestCase):
    def test_new_issue_invalidates_frozen_dispatch(self):
        m=mapping()
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'source_map.json';write(path,m);plan(t,{'provider':'host'},True)
            m['issues']=[{'id':'r','page':1,'check':'new-classification-concern'}];write(path,m)
            with self.assertRaises(LayoutError):translate(t,{'provider':'host'})
    def test_missing_original_image_protection_rejected(self):
        m=mapping();inspection={'pages':m['pages'],'records':[{'id':'image','type':'figure','page':1,'bbox':[300,200,400,300]}]}
        self.assertIn('unprotected-source-image',[i['check'] for i in preflight(m,inspection)['issues']])
        m['pages'][0]['protected']=[{'id':'image','rect':[300,200,400,300]}]
        self.assertTrue(preflight(m,inspection)['passed'])
    def test_new_workflow_requires_inspection(self):
        m=mapping();m['workflow_version']='4.1'
        with tempfile.TemporaryDirectory() as t:
            write(Path(t)/'source_map.json',m)
            with self.assertRaises(LayoutError):plan(t,{'provider':'host'},True)
    def test_dropcap_and_full_line_do_not_move_after_following_paragraph(self):
        m=mapping();m['blocks']=[{'id':'drop','source_page':1,'rect':[30,100,50,130],'size':30,'role':'inline-fragment','inline_fragment_of':['body'],'source_text':'M'},
                                {'id':'body','source_page':1,'rect':[51,103,280,220],'size':10,'source_text':'ost solutions.'},
                                {'id':'whole','source_page':1,'rect':[30,240,280,252],'size':10,'role':'inline-fragment','inline_fragment_of':['next'],'source_text':'2.0 + 3.0'},
                                {'id':'next','source_page':1,'rect':[30,250,280,340],'size':10,'source_text':'Next paragraph.'}]
        self.assertEqual(source_order(m),['drop','body','whole','next'])
    def test_drag_source_snapshot_survives_removal(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'drag.pdf';work=Path(t)/'run'
            with pymupdf.open() as pdf:
                page=pdf.new_page();page.insert_text((40,100),'A paragraph with normal source words.');pdf.save(source)
            expected=source.read_bytes();m,jobs=prepare(source,work,{'provider':'host'})
            source.unlink()
            self.assertEqual((work/'source.pdf').read_bytes(),expected)
            self.assertEqual(Path(m['source_path']), (work/'source.pdf').resolve())
            self.assertFalse(jobs)

    def test_missing_text_cannot_hide_under_original_id(self):
        m=mapping();m['blocks'][0]['original_ids']=['original'];m['blocks'][0]['source_text']='Right'
        inspection={'pages':m['pages'],'records':[{'id':'original','text':'Right paragraph.', 'page':1,'bbox':[320,80,550,110]}]}
        self.assertIn('source-text-coverage',[i['check'] for i in preflight(m,inspection)['issues']])

    def test_columns_heading_and_compiled_order(self):
        m=mapping();self.assertEqual(source_order(m),['heading','body','r'])
        layout=compile_layout(m);self.assertTrue(audit_order(m,layout)['passed'])
        layout['pages'][0]['regions'].reverse();self.assertFalse(audit_order(m,layout)['passed'])
    def test_protected_overlap_blocks_before_translation(self):
        m=mapping();m['pages'][0]['protected']=[{'id':'figure','rect':[40,130,80,160]}]
        self.assertFalse(preflight(m)['ready_to_dispatch'])
        with tempfile.TemporaryDirectory() as t:
            write(Path(t)/'source_map.json',m)
            with self.assertRaises(LayoutError):plan(t,{'provider':'host'},True)
            self.assertFalse((Path(t)/'jobs.json').exists())
    def test_specific_review_and_stale_plan(self):
        m=mapping();m['issues']=[{'id':'r','page':1,'check':'formula-with-prose-candidate'}]
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'source_map.json';write(path,m)
            with self.assertRaises(LayoutError):plan(t,{'provider':'host'},True)
            m['review_resolutions']={'r|formula-with-prose-candidate|1':'Reviewed original: prose line wrap, not a displayed equation.'};write(path,m)
            self.assertTrue(plan(t,{'provider':'host'},True))
            m['blocks'][0]['rect'][1]+=1;write(path,m)
            with self.assertRaises(LayoutError):translate(t,{'provider':'host'})
    def test_duplicate_and_missing_ids(self):
        m=mapping();m['blocks'].append(deepcopy(m['blocks'][0]));self.assertFalse(preflight(m)['passed'])
        m=mapping();m['blocks'].pop();self.assertFalse(preflight(m)['passed'])
    def test_native_text_overlap_and_targets(self):
        m=mapping();m['blocks'].append({'id':'formula','source_page':1,'rect':[40,130,80,160],
            'source_text':'E=mc2','translation_required':False,'preservation_reason':'native-formula'})
        m['expected_block_ids'].append('formula');self.assertFalse(preflight(m)['passed'])
        m=mapping();m['targets']={'ref/1':{'source_page':2,'point':[0,0]}};self.assertFalse(preflight(m)['passed'])
    def test_fingerprint_ignores_translation_and_fonts(self):
        m=mapping();key=fingerprint(m);m['blocks'][0]['translation']='修订';m['fonts']={'regular':'x'}
        self.assertEqual(key,fingerprint(m));m['blocks'][0]['source_text']='New source.';self.assertNotEqual(key,fingerprint(m))
    def test_cross_page_and_full_width_bands(self):
        m=mapping();m['blocks'].append({'id':'full','source_page':1,'rect':[30,200,550,230],'source_text':'Full width paragraph.'})
        m['pages'].append({'source_page':2,'rect':[0,0,600,800]})
        m['blocks'].append({'id':'next','source_page':2,'rect':[30,30,260,90],'source_text':'Continuation.'})
        self.assertEqual(source_order(m),['heading','body','r','full','next'])

if __name__=='__main__':unittest.main()
