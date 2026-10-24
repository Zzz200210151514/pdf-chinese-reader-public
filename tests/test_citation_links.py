import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.citations import (
    author_year_colours, caption_title_ink, dominant_link_blue, object_colours,
    restore_citations,
)
from core.layout import _markup, compile_layout

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
        markup=_markup('见图 2 和[12]',{'fig/2','ref/12'},colours, link_blue='#0d54a6')
        self.assertIn('color:#0055dd',markup)
        self.assertIn('color:#0d54a6',markup)
        self.assertNotIn('#0000ff',markup)

    def test_split_spans_and_bracket_citation_use_number_colour(self):
        colours=object_colours([{'spans':[{'text':'Figure ', 'color':0}, {'text':'2', 'color':0x1256bb},
                                         {'text':' and [','color':0}, {'text':'12','color':0x3456ab}, {'text':']','color':0}]}], {'fig/2','ref/12'})
        self.assertEqual(colours,{'fig/2':'#1256bb','ref/12':'#3456ab'})

    def test_existing_explicit_reference_is_not_corrupted(self):
        text,tokens,colours,issues=restore_citations('已有研究[[ref:12]]',native(),{'ref/12'})
        self.assertFalse(issues)
        self.assertIn('invalid/ref/12',_markup(text,{'ref/12'},colours,tokens))

    def test_pdf_renderer_preserves_anchor_colour_and_link(self):
        import tempfile
        import pymupdf as fitz
        import importlib.util
        spec=importlib.util.spec_from_file_location('citation_pdf_writer',Path(__file__).resolve().parents[1]/'scripts/pdf_reader.py')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
        writer=importlib.util.module_from_spec(spec);spec.loader.exec_module(writer)
        windows = Path('C:/Windows/Fonts')
        manifest = {'link_blue': '#0D54A6'}
        if (windows / 'msyh.ttc').is_file():
            css, archive = writer.style(manifest, str(windows))
            self._assert_anchor_css(css)
            self._assert_citation_colour(fitz, css, archive)
            return
        # Linux agents have no Microsoft YaHei. The colour lives in the link
        # markup; any face that can draw the digits is enough to read it back.
        with tempfile.TemporaryDirectory() as directory:
            font_dir = Path(directory)
            (font_dir / 'msyh.ttc').write_bytes(fitz.Font('cjk').buffer)
            css, archive = writer.style(
                {**manifest, 'fonts': {'regular': 'msyh.ttc', 'bold': 'msyh.ttc', 'fallback': None}},
                str(font_dir))
            self._assert_anchor_css(css)
            self._assert_citation_colour(fitz, css, archive)

    def _assert_anchor_css(self, css):
        self.assertIn('a{color:#0d54a6;', css)
        self.assertNotIn('#0000ff', css)

    def _assert_citation_colour(self, fitz, css, archive):
        marked, tokens, colours, issues = restore_citations('Evidence 12', native(), {'ref/12'})
        with fitz.open() as doc:
            page=doc.new_page()
            page.insert_htmlbox(fitz.Rect(40,40,350,100),_markup(marked,{'ref/12'},colours,tokens),css=css,archive=archive)
            page=doc.reload_page(page)
            links=page.get_links()
            self.assertTrue(any(l.get('uri','').endswith('/ref/12') for l in links))
            spans=[s for b in page.get_text('dict')['blocks'] for l in b.get('lines',[]) for s in l['spans']]
            citation=next(s for s in spans if s['text'].strip()=='12')
            self.assertEqual(citation['color'],0x2156aa)

    def test_author_year_keeps_source_link_blue(self):
        lines=[{'spans':[{'text':'Nijmeijer and Metz, 2010','color':0x004c94}]}]
        ay=author_year_colours(lines)
        self.assertEqual(ay,{'Nijmeijer and Metz, 2010':'#004c94'})
        markup=_markup('结构（Nijmeijer and Metz, 2010）。',set(),{},(),ay)
        self.assertIn('style="color:#004c94;"',markup)
        self.assertIn('Nijmeijer and Metz, 2010',markup)
        self.assertNotIn('<a ',markup)

    def test_figure_still_linked_beside_author_year(self):
        lines=[{'spans':[
            {'text':'Nijmeijer and Metz, 2010','color':0x004c94},
            {'text':'Figure 1a','color':0x004c94},
        ]}]
        ay=author_year_colours(lines)
        markup=_markup('（Nijmeijer and Metz, 2010）见图 1',{'fig/1'},{'fig/1':'#004c94'},(),ay)
        self.assertIn('invalid/fig/1',markup)
        self.assertIn('<span style="color:#004c94;">Nijmeijer and Metz, 2010</span>',markup)

    def test_cross_line_figure_uses_digit_link_blue(self):
        # `...(Figure` at the end of one line and `2a).` on the next.
        lines = [
            {'spans': [{'text': 'shown in (Figure', 'color': 0x000000}]},
            {'spans': [{'text': '2', 'color': 0x0d54a6}, {'text': 'a). ', 'color': 0x000000}]},
        ]
        colours = object_colours(lines, {'fig/2'})
        self.assertEqual(colours, {'fig/2': '#0d54a6'})
        markup = _markup('见图 2a', {'fig/2'}, colours)
        self.assertIn('color:#0d54a6', markup)
        self.assertIn('invalid/fig/2', markup)
        self.assertNotIn('#0000ff', markup)

    def test_black_caption_title_does_not_recolour_body_cross_refs(self):
        caption = [
            {'spans': [{'text': 'Figure', 'color': 0x000000}]},
            {'spans': [{'text': '2. Observed spectra.', 'color': 0x000000}]},
        ]
        body = [{'spans': [{'text': 'See Fig. 2 and Fig. 2.', 'color': 0x000000}]}]
        # Black caption ink must not enter the object map, alone or beside body ink.
        self.assertEqual(object_colours(caption, {'fig/2'}), {})
        self.assertEqual(object_colours(caption + body, {'fig/2'}), {})
        self.assertEqual(caption_title_ink(caption), ('fig/2', '#000000'))
        self.assertEqual(caption_title_ink([
            {'spans': [{'text': 'Table 1. Conditions.', 'color': 0}]},
        ]), ('table/1', '#000000'))
        blue_digit = [
            {'spans': [{'text': 'See (Figure', 'color': 0}]},
            {'spans': [{'text': '2a).', 'color': 0x0d54a6}]},
        ]
        self.assertEqual(object_colours(caption + blue_digit, {'fig/2'}), {'fig/2': '#0d54a6'})
        self.assertEqual(object_colours(blue_digit + caption, {'fig/2'}), {'fig/2': '#0d54a6'})
        # A link-blue caption title is source colour, not forced black.
        self.assertEqual(caption_title_ink([
            {'spans': [{'text': 'Figure 2. Spectra.', 'color': 0x0d54a6}]},
        ]), (None, None))
        document = caption + [
            {'spans': [{'text': '1', 'color': 0x0d54a6}, {'text': '2', 'color': 0x0d54a6}]},
        ]
        blue = dominant_link_blue(document)
        self.assertEqual(blue, '#0d54a6')
        body_markup = _markup('见图 2 与 Fig. 2', {'fig/2'}, object_colours(caption + body, {'fig/2'}),
                              link_blue=blue)
        self.assertEqual(body_markup.count('color:#0d54a6'), 2)
        self.assertNotIn('#000000', body_markup)
        self.assertNotIn('#0000ff', body_markup)
        # Stored caption black is not a usable link colour for body refs either.
        forced = _markup('见图 2', {'fig/2'}, {'fig/2': '#000000'}, link_blue=blue)
        self.assertIn('color:#0d54a6', forced)
        self.assertNotIn('#000000', forced)
        title = _markup('<b>图 2</b>. 光谱见图 2a。', {'fig/2'}, {}, link_blue=blue,
                        caption_key='fig/2', caption_ink='#000000')
        self.assertEqual(title.count('color:#000000'), 1)
        self.assertEqual(title.count('color:#0d54a6'), 1)
        self.assertNotIn('#0000ff', title)
        mixed = _markup('<b>图 2</b>. 见图 3。', {'fig/2', 'fig/3'}, {'fig/3': '#0d54a6'},
                        link_blue=blue, caption_key='fig/2', caption_ink='#000000')
        self.assertIn('color:#000000', mixed)
        self.assertIn('color:#0d54a6', mixed)
        self.assertNotIn('#0000ff', mixed)

    def test_missing_colour_falls_back_to_dominant_link_blue(self):
        lines = [
            {'spans': [{'text': 'Figure 2. ' * 8, 'color': 0x000000}]},
            {'spans': [{'text': '1', 'color': 0x0d54a6}, {'text': '2', 'color': 0x0d54a6},
                       {'text': '3', 'color': 0x0d54a6}]},
            {'spans': [{'text': 'once', 'color': 0x0000ff}]},
        ]
        self.assertEqual(dominant_link_blue(lines), '#0d54a6')
        self.assertIsNone(dominant_link_blue([{'spans': [{'text': 'Figure 1.', 'color': 0}]}]))
        text, tokens, colours, issues = restore_citations('已有研究。12', native(colour=0), {'ref/12'})
        self.assertFalse(issues)
        self.assertEqual(colours, {})
        markup = _markup(text, {'ref/12'}, colours, tokens, link_blue=dominant_link_blue(lines))
        self.assertIn('<sup><a ', markup)
        self.assertIn('color:#0d54a6', markup)
        self.assertNotIn('#0000ff', markup)
        bare = _markup('见[12]和图 4', {'ref/12', 'fig/4'}, {})
        self.assertNotIn('#0000ff', bare)
        self.assertNotIn('color:', bare)
        styled = _markup('见[12]和图 4', {'ref/12', 'fig/4'}, {}, link_blue='#0d54a6')
        self.assertEqual(styled.count('color:#0d54a6'), 2)
        self.assertNotIn('#0000ff', styled)

    def test_compiled_blocks_keep_caption_black_and_body_link_blue(self):
        acs = 0x0d54a6

        def block(ident, y0, y1, text, translation, kind='paragraph', lines=None):
            return {'id': ident, 'source_page': 1, 'rect': [40, y0, 300, y1],
                    'source_text': text, 'translation': translation, 'translation_required': True,
                    'type': kind, 'source_lines': lines}

        ink = [{'spans': [{'text': '1', 'color': acs}, {'text': '2', 'color': acs},
                          {'text': '3', 'color': acs}]}]
        cross = [
            {'spans': [{'text': 'shown in (Figure', 'color': 0}]},
            {'spans': [{'text': '2', 'color': 0x1256bb}, {'text': 'a).', 'color': 0}]},
        ]
        plain = [{'spans': [{'text': 'See Fig. 3 in the text.', 'color': 0}]}]
        caption = [
            {'spans': [{'text': 'Figure', 'color': 0}]},
            {'spans': [{'text': '2. Spectra of the sample.', 'color': 0}]},
        ]
        blocks = [
            block('ink', 40, 70, '11 22 33', '参考文献着色。', lines=ink),
            block('cross', 90, 140, 'shown in (Figure\n2a).', '见图 2a。', lines=cross),
            block('plain', 160, 220, 'See Fig. 3 in the text.', '见图 3 与 Fig. 3。', lines=plain),
            block('cap', 400, 460, 'Figure\n2. Spectra of the sample.', '<b>图 2</b>. 光谱。',
                  kind='caption', lines=caption),
        ]
        mapping = {'layout_reviewed': True, 'source_sha256': 'a' * 64,
                   'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800], 'protected': []}],
                   'targets': {'fig/2': {'source_page': 1, 'point': [40, 400]},
                               'fig/3': {'source_page': 1, 'point': [40, 160]}},
                   'references': [], 'blocks': blocks,
                   'expected_block_ids': [item['id'] for item in blocks]}
        manifest = compile_layout(mapping)
        self.assertEqual(manifest['link_blue'], '#0d54a6')
        html = {region['id']: region['html'] for region in manifest['pages'][0]['regions']}
        self.assertIn('color:#1256bb', html['cross'])
        self.assertNotIn('#0000ff', html['cross'])
        self.assertNotIn('#000000', html['cross'])
        self.assertEqual(html['plain'].count('color:#0d54a6'), 2)
        self.assertNotIn('#000000', html['plain'])
        self.assertNotIn('#0000ff', html['plain'])
        self.assertIn('color:#000000', html['cap'])
        self.assertNotIn('#0d54a6', html['cap'])
        self.assertNotIn('#1256bb', html['cap'])
        self.assertNotIn('#0000ff', html['cap'])

    def test_anchor_stylesheet_does_not_invent_pure_blue(self):
        import importlib.util
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        spec = importlib.util.spec_from_file_location(
            'citation_css_writer', Path(__file__).resolve().parents[1] / 'scripts/pdf_reader.py')
        writer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(writer)
        self.assertEqual(writer.anchor_color_rule({'link_blue': '#0D54A6'}), 'color:#0d54a6;')
        self.assertEqual(writer.anchor_color_rule({}), '')
        self.assertEqual(writer.anchor_color_rule({'link_blue': '#0000ff'}), 'color:#0000ff;')
        self.assertNotIn('0000ff', writer.anchor_color_rule({}))
        self.assertNotIn('0000ff', writer.anchor_color_rule({'link_blue': 'blue'}))

if __name__=='__main__':unittest.main()
