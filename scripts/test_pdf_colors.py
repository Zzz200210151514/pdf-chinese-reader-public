"""Offline regression checks for explicit Separation tints after redaction."""
import unittest
import pymupdf as fitz
from pdf_colors import capture_separation_tints, restore_separation_tints


def fixture(stream):
    doc=fitz.open();page=doc.new_page(width=200,height=200)
    function=doc.get_new_xref()
    doc.update_object(function,'<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0 0.05 0.2 0] /N 1 >>')
    color=doc.get_new_xref();doc.update_object(color,f'[/Separation /Tint /DeviceCMYK {function} 0 R]')
    doc.xref_set_key(page.xref,'Resources',f'<< /ColorSpace << /Tint {color} 0 R >> >>')
    content=doc.get_new_xref();doc.update_object(content,'<<>>');doc.update_stream(content,stream)
    page.set_contents(content)
    return doc,page,content


class ColorTests(unittest.TestCase):
    def test_restore_missing_tint_and_preserve_pixels(self):
        doc,page,xref=fixture(b'q /Tint cs 1 scn 10 10 100 100 re f Q')
        before=page.get_pixmap().samples;captured=capture_separation_tints(page)
        doc.update_stream(xref,b'q /Tint cs 10 10 100 100 re f Q')
        repairs=restore_separation_tints(page,captured)
        self.assertEqual(len(repairs),1)
        self.assertIn(b'/Tint cs 1 scn',doc.xref_stream(xref))
        self.assertEqual(before,page.get_pixmap().samples)
        self.assertEqual(restore_separation_tints(page,captured),[])
        doc.close()

    def test_explicit_nondefault_and_text_are_not_rewritten(self):
        content=b'q /Tint cs .3 scn 10 10 100 100 re f Q (/Tint cs (nested) escaped\\)) Tj'
        doc,page,xref=fixture(content)
        captured=capture_separation_tints(page)
        self.assertEqual(restore_separation_tints(page,captured),[])
        self.assertEqual(doc.xref_stream(xref),content)
        doc.close()

    def test_ambiguous_original_tints_stop_instead_of_guessing(self):
        doc,page,xref=fixture(b'/Tint cs .3 scn 10 10 20 20 re f /Tint cs 1 scn 60 60 20 20 re f')
        captured=capture_separation_tints(page)
        doc.update_stream(xref,b'/Tint cs 10 10 20 20 re f')
        with self.assertRaisesRegex(ValueError,'ambiguous'):restore_separation_tints(page,captured)
        doc.close()

    def test_actual_redaction_restores_original_default(self):
        doc,page,xref=fixture(b'q /Tint cs 1 scn 10 10 100 100 re f Q')
        before=page.get_pixmap().samples;captured=capture_separation_tints(page)
        page.add_redact_annot(fitz.Rect(20,20,40,40),fill=None,cross_out=False)
        page.apply_redactions(images=0,graphics=0,text=0)
        restore_separation_tints(page,captured)
        content=b'\n'.join(doc.xref_stream(x) for x in page.get_contents())
        self.assertIn(b'/Tint cs 1 scn',content)
        self.assertEqual(before,page.get_pixmap().samples)
        doc.close()

    def test_unknown_implicit_source_tint_is_not_guessed(self):
        doc,page,xref=fixture(b'/Tint cs .3 scn 10 10 20 20 re f /Tint cs 60 60 20 20 re f')
        captured=capture_separation_tints(page)
        with self.assertRaisesRegex(ValueError,'ambiguous'):restore_separation_tints(page,captured)
        doc.close()


if __name__=='__main__':unittest.main()
