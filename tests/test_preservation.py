import sys,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
import pymupdf as fitz
from core.preservation import document_copy,color_profiles,audit_document
from pdf_reader import build

class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source.pdf';self.output=self.root/'translated.pdf'
        (self.root/'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)
        d=fitz.open()
        for i in range(2):
            p=d.new_page(width=300,height=400);p.insert_text((30,70),'Source prose with 10 mL at 25 C.',fontsize=9)
            p.draw_rect(fitz.Rect(160,110,240,180),color=(.1,.5,.7),fill=(.2,.4,.6))
            d.xref_set_key(p.xref,'Group','<< /Type /Group /S /Transparency /CS /DeviceRGB >>')
        d[0].insert_link({'kind':fitz.LINK_URI,'from':fitz.Rect(30,55,140,75),'uri':'https://doi.org/10.0000/fixture'})
        d[0].insert_link({'kind':fitz.LINK_GOTO,'from':fitz.Rect(30,55,90,75),'page':1,'to':fitz.Point(30,40)})
        d.set_metadata({'title':'Original title','author':'Fixture author','keywords':'science','creator':'Fixture generator'})
        d.set_toc([[1,'Original chapter',1],[2,'Next section',2]])
        # This stream tests catalog/resource identity; real ICC rendering is covered by external regression.
        profile=d.get_new_xref();d.update_object(profile,'<< /N 3 >>');d.update_stream(profile,b'fixture-profile-resource')
        intent=d.get_new_xref();d.update_object(intent,f'<< /Type /OutputIntent /S /GTS_PDFA1 /DestOutputProfile {profile} 0 R >>')
        d.xref_set_key(d.pdf_catalog(),'OutputIntents',f'[{intent} 0 R]');d.save(self.source);d.close()
    def tearDown(self):self.temp.cleanup()
    def test_catalog_page_groups_and_metadata_survive_selection(self):
        with fitz.open(self.source) as original,document_copy(self.source,[2]) as selected:
            self.assertEqual(color_profiles(original),color_profiles(selected))
            self.assertEqual(selected.metadata['author'],'Fixture author')
            self.assertEqual(selected.xref_get_key(selected[0].xref,'Group')[0],'dict')
            self.assertEqual(len(selected),1)
    def test_redaction_preserves_uri_goto_and_bookmarks(self):
        # Fake resource bytes are not a valid rendering profile; real profiles are
        # independently exercised by Han's catalog/color regression.
        with fitz.open(self.source) as d:
            d.xref_set_key(d.pdf_catalog(),'OutputIntents','null')
            d.save(self.root/'renderable.pdf')
        self.source=self.root/'renderable.pdf'
        m={'title':'中文标题','scope':'Fixture full paper','fonts':{'regular':'cjk.ttf'},'pages':[]}
        for n in [1,2]:m['pages'].append({'source_page':n,'redactions':[[29,52,150,80]],'regions':[{'id':f'body{n}','rect':[29,52,150,80],'html':'中文译文 10 mL，25 C。','size':9,'leading':1.2}], 'protected':[{'id':f'plot{n}','rect':[160,110,240,180]}]})
        path=self.root/'layout.json';path.write_text(json.dumps(m,ensure_ascii=False),'utf-8')
        build(SimpleNamespace(source=self.source,manifest=path,output=self.output,font_dir=self.root))
        with fitz.open(self.source) as a,fitz.open(self.output) as b:
            audit=audit_document(a,b,[1,2]);self.assertTrue(audit['passed'],audit)
            self.assertIn('Original chapter',[x[1] for x in b.get_toc()])
            self.assertTrue(any(x['kind']==fitz.LINK_GOTO and x['page']==1 for x in b[0].get_links()))
            self.assertIn('中文译文',b[0].get_text())
            self.assertEqual(len(b),2)
            for link in b[0].get_links():
                if link['kind']==fitz.LINK_GOTO and link['page']==1:
                    b[0].delete_link(link)
            damaged=self.root/'missing-link.pdf';b.save(damaged)
        with fitz.open(self.source) as a,fitz.open(damaged) as b:
            audit=audit_document(a,b,[1,2])
            self.assertFalse(audit['passed'])
            self.assertTrue(audit['missing_internal_links'])

if __name__=='__main__':unittest.main()
