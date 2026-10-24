import json,sys,tempfile,unittest
from pathlib import Path
import pymupdf
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import reflow_reader as reader
import reflow_links as links

class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd());self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name);self.source=self.work/'source.pdf'
        source=pymupdf.open();p=source.new_page();p.draw_rect((30,30,300,250),color=(.2,.3,.6));source.save(self.source);source.close()
        with pymupdf.open(self.source) as source:
            source[0].get_pixmap(clip=pymupdf.Rect(30,30,300,250)).save(self.work/'figure.png')
        self.blocks=[
            {'id':'body','type':'paragraph','source_text':'Body cites [1] and Fig. 3d.','source_page':1,'source_rect':[30,30,300,60]},
            {'id':'figure','type':'figure','number':'3','source_text':'Fig. 3. Six panels.','source_page':1,'source_rect':[30,30,300,250],'crop_rect':[30,30,300,250],'asset':'figure.png'},
            {'id':'entry','type':'paragraph','reference_source':True,'source_text':'[1] A. Example, Paper, 2025.','source_page':1,'source_rect':[30,300,300,340]},
        ]
        self.translations={b['id']:b['source_text'] for b in self.blocks}
        self.reviews={'entry':{'preserve':True},'figure':{'panels':{'d':[0,.5,1/3,1]}}}

    def make_pdf(self):
        doc=pymupdf.open();p=doc.new_page();p.insert_text((40,80),self.translations['body'])
        p=doc.new_page();p.insert_image((40,100,340,300),filename=str(self.work/'figure.png'),keep_proportion=False)
        p.insert_text((40,350),self.translations['figure']);p.insert_text((40,450),self.translations['entry'])
        return doc

    def test_reference_number_itself_is_a_return_link_and_panel_is_actual_image(self):
        with self.make_pdf() as doc:
            report=links.attach(doc,self.blocks,self.translations,self.reviews,self.source,self.work)
            path=self.work/'reader.pdf';doc.save(path)
        with pymupdf.open(path) as doc:
            p,x,y=doc.resolve_link('#fig-3d');self.assertEqual(p,1);self.assertAlmostEqual(x,40,places=2);self.assertAlmostEqual(y,197,places=2)
            label=doc[1].search_for('[1]')[0]
            back=[l for l in doc[1].get_links() if l['from'].intersects(label) and l['kind']==pymupdf.LINK_GOTO]
            self.assertEqual(len(back),1);self.assertEqual(back[0]['page'],0)
            citation=doc[0].search_for('[1]')[0]
            self.assertLess(abs(back[0]['to'].x-citation.x0),5)
            self.assertEqual(report['reference_number_backlinks'],1)
            self.assertEqual(report['reviewed_panel_targets'],1)

    def test_offline_number_backlink_matches_pdf_and_preserves_inequalities(self):
        self.translations['body']='Body cites [1] and Fig. 3d. c < 1 and c > 0.'
        with self.make_pdf() as doc:report=links.attach(doc,self.blocks,self.translations,self.reviews,self.source,self.work)
        html='<html><head></head><body><p id="body">Body cites [1] and Fig. 3d. c &lt; 1 and c &gt; 0.</p><div class="media figure" id="figure"><img src="figure.png"><p class="caption">Fig. 3. Six panels.</p></div><p id="entry">[1] A. Example, Paper, 2025.</p></body></html>'
        output=links.decorate_html(html,self.blocks,self.translations,self.reviews,report,self.work)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('playwright not installed')
        with sync_playwright() as p:
            with p.chromium.launch(headless=True) as browser:
                page=browser.new_page(viewport={'width':1000,'height':500})
                page.set_content(output.replace('<p id="entry">','<div style="height:1400px"></div><p id="entry">'))
                self.assertIn('c < 1 and c > 0.',page.locator('#body').inner_text())
                page.locator('#body a[href="#ref-1"]').click()
                self.assertGreater(page.evaluate('scrollY'),300)
                page.locator('#entry a.ref-back').click()
                self.assertLess(page.evaluate('scrollY'),50)

    def test_inequality_is_scientific_text_not_an_html_tag(self):
        self.assertEqual(reader.compact('c<sup>*</sup> < 1/V, dc/di > 0'),'c*<1/V,dc/di>0')

    def test_print_citation_color_is_visible_and_keeps_number_backlink(self):
        body='<html><head></head><body><p id="body">Body cites [1] and Fig. 3d. c &lt; 1.</p><p id="entry">[1] A. Example, Paper, 2025.</p></body></html>'
        colored=links.style_print_citations(body,self.blocks,self.translations,self.reviews)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('playwright not installed')
        with sync_playwright() as runtime:
            with runtime.chromium.launch(headless=True) as browser:
                page=browser.new_page();page.set_content(colored)
                data=page.pdf()
        with pymupdf.open(stream=data,filetype='pdf') as doc:
            blue=[s['text'] for p in doc for b in p.get_text('dict')['blocks'] for l in b.get('lines',[]) for s in l['spans'] if s['color']==0x2196d1]
            self.assertEqual(blue,['1','3d','[1]'],[(s['text'],hex(s['color'])) for p in doc for b in p.get_text('dict')['blocks'] for l in b.get('lines',[]) for s in l['spans']])
            self.assertIn('c < 1.',doc[0].get_text())
        with self.make_pdf() as doc:report=links.attach(doc,self.blocks,self.translations,self.reviews,self.source,self.work)
        html=links.decorate_html(colored,self.blocks,self.translations,self.reviews,report,self.work)
        self.assertIn('class="ref-back" href="#body"',html)

    def test_chart_panels_and_reference_blocks_are_not_dropped(self):
        raw=[{'type':'chart','page_idx':0,'bbox':[10,10,400,300],'chart_caption':[]},
             {'type':'chart','page_idx':0,'bbox':[450,10,900,300],'chart_caption':['Fig. 3. Two panels.']},
             {'type':'ref_text','page_idx':0,'bbox':[10,500,900,600],'text':'[1] Original reference.'}]
        adapted=reader.normalize_items(raw)
        self.assertEqual(len(adapted),2);self.assertEqual(adapted[0]['bbox'],[10,10,900,300])
        self.assertEqual(adapted[0]['source_panel_indices'],[0,1]);self.assertTrue(adapted[1]['reference_source'])
        self.assertEqual(raw[0]['type'],'chart')

if __name__=='__main__':unittest.main()
