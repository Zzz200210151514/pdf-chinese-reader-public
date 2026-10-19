import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.citations import restore_citations, object_colours
from core.layout import _markup

def native(text='12', colour=0x2156aa):
    return [{'spans':[{'text':'Earlier studies.', 'size':10, 'origin':[0,20], 'color':0},
                      {'text':text, 'size':7, 'origin':[60,16], 'color':colour, 'flags':1}]}]

class CitationLinks(unittest.TestCase):
    def test_native_superscript_gets_link_and_exact_colour(self):
        known={'ref/12'}
        text,tokens,colours,issues=restore_citations('已有研究。12',native(),known)
        self.assertFalse(issues)
        markup=_markup(text,known,colours,tokens)
        self.assertIn('<sup><a ',markup)
        self.assertIn('invalid/ref/12',markup)
        self.assertIn('color:#2156aa',markup)

    def test_group_and_range_keep_text_and_separate_targets(self):
        known={'ref/'+str(n) for n in (1,3,5)}
        text,tokens,colours,issues=restore_citations('结论。1，3–5',native('1,3−5'),known)
        self.assertFalse(issues)
        markup=_markup(text,known,colours,tokens)
        for n in (1,3,5):self.assertIn('invalid/ref/'+str(n),markup)
        self.assertIn('，',markup)
        self.assertIn('–',markup)

    def test_normal_numbers_and_chemical_power_not_guessed(self):
        self.assertEqual(restore_citations('12 mg',[],{'ref/12'})[0],'12 mg')
        lines=[{'spans':[{'text':'x','size':10,'origin':[0,20],'color':0},
                         {'text':'2','size':7,'origin':[5,16],'color':0,'flags':1}]}]
        self.assertEqual(restore_citations('x2',lines,{'ref/2'})[0],'x2')

    def test_ambiguous_bare_number_requires_review(self):
        text,_,_,issues=restore_citations('12 次，见研究12',native(),{'ref/12'})
        self.assertTrue(issues)
        self.assertNotIn('<sup>',text)

    def test_explicit_superscript_disambiguates_normal_number(self):
        text,tokens,colours,issues=restore_citations('12 次，见研究<sup>12</sup>',native(),{'ref/12'})
        self.assertFalse(issues)
        markup=_markup(text,{'ref/12'},colours,tokens)
        self.assertEqual(markup.count('invalid/ref/12'),1)
        self.assertTrue(markup.startswith('12 次'))

    def test_native_figure_colour_and_default_link_colour(self):
        colours=object_colours([{'spans':[{'text':'Fig. 2','color':0x0055dd}]}],{'fig/2'})
        markup=_markup('见图 2 和[12]',{'fig/2','ref/12'},colours)
        self.assertIn('color:#0055dd',markup)
        self.assertIn('color:#0000ff',markup)

    def test_split_spans_and_bracket_citation_use_number_colour(self):
        colours=object_colours([{'spans':[{'text':'Figure ', 'color':0}, {'text':'2', 'color':0x1256bb},
                                         {'text':' and [','color':0}, {'text':'12','color':0x3456ab}, {'text':']','color':0}]}], {'fig/2','ref/12'})
        self.assertEqual(colours,{'fig/2':'#1256bb','ref/12':'#3456ab'})

    def test_existing_explicit_reference_is_not_corrupted(self):
        text,tokens,colours,issues=restore_citations('已有研究[[ref:12]]',native(),{'ref/12'})
        self.assertFalse(issues)
        self.assertIn('invalid/ref/12',_markup(text,{'ref/12'},colours,tokens))

    def test_pdf_renderer_preserves_anchor_colour_and_link(self):
        import pymupdf as fitz
        import importlib.util
        spec=importlib.util.spec_from_file_location('citation_pdf_writer',Path(__file__).resolve().parents[1]/'scripts/pdf_reader.py')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
        writer=importlib.util.module_from_spec(spec);spec.loader.exec_module(writer)
        css,archive=writer.style({},'C:/Windows/Fonts')
        marked,tokens,colours,issues=restore_citations('Evidence 12',native(),{'ref/12'})
        with fitz.open() as doc:
            page=doc.new_page()
            page.insert_htmlbox(fitz.Rect(40,40,350,100),_markup(marked,{'ref/12'},colours,tokens),css=css,archive=archive)
            page=doc.reload_page(page)
            links=page.get_links()
            self.assertTrue(any(l.get('uri','').endswith('/ref/12') for l in links))
            spans=[s for b in page.get_text('dict')['blocks'] for l in b.get('lines',[]) for s in l['spans']]
            citation=next(s for s in spans if s['text'].strip()=='12')
            self.assertEqual(citation['color'],0x2156aa)

if __name__=='__main__':unittest.main()
