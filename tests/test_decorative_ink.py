"""Synthetic embedded outlines; decorative ink never inferred from font size."""
import copy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.pdf_reader import annotate_decorative_ink,_cff_outline_bounds

try:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
except ImportError:
    FontBuilder=None


def marker_font():
    builder=FontBuilder(1000,isTTF=False)
    builder.setupGlyphOrder(['.notdef','filledbox'])
    builder.setupCharacterMap({9632:'filledbox'})
    pen=T2CharStringPen(312,None)
    pen.moveTo((50,125));pen.lineTo((269,125));pen.lineTo((269,350));pen.lineTo((50,350));pen.closePath()
    empty=T2CharStringPen(312,None)
    builder.setupCFF('SyntheticMarker',{'FullName':'SyntheticMarker','FamilyName':'SyntheticMarker',
        'Weight':'Regular','FontBBox':[0,-52,489,687]},
        {'.notdef':empty.getCharString(),'filledbox':pen.getCharString()}, {})
    return builder.font['CFF '].compile(builder.font)


@unittest.skipIf(FontBuilder is None,'Optional fontTools native-outline support unavailable')
class DecorativeInkTests(unittest.TestCase):
    def fixture(self,heading='METHODS',glyph_id=1):
        marker={'text':'■','font':'SyntheticMarker','size':32,'origin':(50,100),
            'bbox':[50,74.4,59.984,106.4],'nonwhitespace_bbox':[50,74.4,59.984,106.4]}
        other={'text':heading,'size':10,'flags':16,'bbox':[65,87,140,99],
               'nonwhitespace_bbox':[65,87,140,99]}
        line={'dir':(1,0),'spans':[marker,other],'bbox':[50,74.4,140,106.4]}
        block={'type':0,'bbox':list(line['bbox']),'lines':[line]}
        trace={'font':'SyntheticMarker','size':32,'dir':(1,0),'wmode':0,'type':0,
               'chars':[(9632,glyph_id,(50,100),(50,74.4,59.984,106.4))]}
        page=SimpleNamespace(get_texttrace=lambda:[trace],
            get_fonts=lambda full:[(42,'cff','Type1','ABCDEF+SyntheticMarker','Marker','')],
            parent=SimpleNamespace(extract_font=lambda xref:('SyntheticMarker','cff','Type1',marker_font())))
        return page,block,marker,line,trace

    def test_embedded_outline_not_global_font_bbox(self):
        outline=_cff_outline_bounds(marker_font(),1)
        self.assertEqual(outline[0],(50,125,269,350))
        self.assertEqual(outline[2],312)
        self.assertEqual(outline[3],'filledbox')

    def test_exact_native_geometry_and_legacy_bounds_retained(self):
        page,block,marker,line,trace=self.fixture()
        legacy=copy.deepcopy(marker['bbox'])
        annotate_decorative_ink(page,[block])
        self.assertEqual(marker['bbox'],legacy)
        for actual,expected in zip(marker['nonwhitespace_bbox'],[51.6,88.8,58.608,96]):
            self.assertAlmostEqual(actual,expected,places=5)
        self.assertEqual(marker['native_ink_proof']['glyph_id'],1)
        self.assertEqual(line['nonwhitespace_bbox'],[51.6,87,140,99])

    def test_formula_and_greek_symbols_never_reduced(self):
        for heading in ['K = ABC','ΔG','A + B','X ∑ Y']:
            page,block,marker,line,trace=self.fixture(heading)
            annotate_decorative_ink(page,[block])
            self.assertNotIn('native_ink_proof',marker)

    def test_unknown_glyph_rotated_and_changed_width_fail_closed(self):
        for alteration in ['glyph','rotation','width','stroke','font']:
            page,block,marker,line,trace=self.fixture(glyph_id=99 if alteration=='glyph' else 1)
            if alteration=='rotation':trace['dir']=(0,1)
            if alteration=='width':trace['chars']=[(9632,1,(50,100),(50,74.4,80,106.4))]
            if alteration=='stroke':trace['type']=1
            if alteration=='font':page.get_fonts=lambda full:[]
            annotate_decorative_ink(page,[block])
            self.assertNotIn('native_ink_proof',marker)


if __name__=='__main__':unittest.main()
