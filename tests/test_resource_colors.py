import sys
import re
import hashlib
import unittest
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.resource_colors import snapshot_default_spaces, restore_default_spaces


def set_resource(doc, xref, key, value):
    kind, resource = doc.xref_get_key(xref, "Resources")
    if kind == "xref":
        doc.xref_set_key(int(resource.split()[0]), key, value)
    else:
        doc.xref_set_key(xref, "Resources/" + key, value)


def fixture():
    source = fitz.open()
    p = source.new_page(width=200, height=200)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
    pix.clear_with(128)
    p.insert_image(fitz.Rect(20, 20, 120, 120), pixmap=pix)
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.show_pdf_page(page.rect, source, 0)
    page.insert_text((20, 170), "remove this text")
    doc.xref_set_key(page.get_images(full=True)[0][0], "ColorSpace", "/DeviceRGB")
    image_form = next(x[0] for x in page.get_xobjects()
                      if doc.xref_get_key(x[0], "Resources/XObject")[0] != "null"
                      and "/Image" in " ".join(doc.xref_object(int(ref)) for ref in
                          re.findall(r"(\d+)\s+\d+\s+R", doc.xref_get_key(x[0], "Resources/XObject")[1])))
    color = doc.get_new_xref()
    doc.update_object(color, "[/CalRGB << /WhitePoint [0.9505 1 1.089] /Gamma [2 2 2] >>]")
    spaces = doc.get_new_xref()
    doc.update_object(spaces, f"<< /DefaultRGB {color} 0 R /KeepMe /DeviceRGB >>")
    set_resource(doc, image_form, "ColorSpace", f"{spaces} 0 R")
    source.close()
    return doc, page, color


def pixel_hash(doc, clip):
    with fitz.open(stream=doc.tobytes(), filetype="pdf") as fresh:
        return hashlib.sha256(fresh[0].get_pixmap(clip=clip).samples).hexdigest()


class ResourceColorTests(unittest.TestCase):
    def test_redaction_clean_restore_form_and_pixels(self):
        doc, page, color = fixture()
        clip = fitz.Rect(20, 20, 120, 120)
        before_pixels = pixel_hash(doc, clip)
        snapshot = snapshot_default_spaces(page)
        self.assertTrue(snapshot['forms'])
        page.add_redact_annot(fitz.Rect(10, 150, 190, 180), fill=False)
        page.apply_redactions(images=0, graphics=0)
        page = doc.reload_page(page)
        cleaned = snapshot_default_spaces(page)
        self.assertFalse(cleaned['forms'])
        self.assertNotEqual(pixel_hash(doc, clip), before_pixels)
        self.assertGreater(restore_default_spaces(page, snapshot), 0)
        page = doc.reload_page(page)
        restored = snapshot_default_spaces(page)
        self.assertEqual(restored['forms'][0]['spaces']['DefaultRGB'], f"{color} 0 R")
        self.assertEqual(pixel_hash(doc, clip), before_pixels)
        self.assertEqual(restore_default_spaces(page, snapshot), 0)
        doc.close()

    def test_merge_existing_colors_and_root_defaults(self):
        doc, page, color = fixture()
        set_resource(doc, page.xref, "ColorSpace", "<< /DefaultGray /DeviceGray /Other /DeviceRGB >>")
        snapshot = snapshot_default_spaces(page)
        set_resource(doc, page.xref, "ColorSpace/DefaultGray", "null")
        form = snapshot['forms'][0]['xref']
        set_resource(doc, form, "ColorSpace", "<< /NewEntry /DeviceCMYK >>")
        restore_default_spaces(page, snapshot)
        self.assertEqual(doc.xref_get_key(page.xref, "Resources/ColorSpace/Other")[1], "/DeviceRGB")
        self.assertEqual(doc.xref_get_key(page.xref, "Resources/ColorSpace/DefaultGray")[1], "/DeviceGray")
        self.assertEqual(doc.xref_get_key(form, "Resources/ColorSpace/NewEntry")[1], "/DeviceCMYK")
        self.assertEqual(doc.xref_get_key(form, "Resources/ColorSpace/DefaultRGB")[1], f"{color} 0 R")
        doc.close()

    def test_different_document_rejected(self):
        doc, page, _ = fixture()
        other = fitz.open()
        other_page = other.new_page()
        with self.assertRaises(ValueError):
            restore_default_spaces(other_page, snapshot_default_spaces(page))
        other.close()
        doc.close()

    def test_all_default_spaces_and_inherited_page_resources(self):
        doc, page, _ = fixture()
        set_resource(doc, page.xref, "ColorSpace", "<< /DefaultRGB /DeviceRGB /DefaultCMYK /DeviceCMYK /DefaultGray /DeviceGray /Other /DeviceRGB >>")
        kind, resource = doc.xref_get_key(page.xref, "Resources")
        parent = int(doc.xref_get_key(page.xref, "Parent")[1].split()[0])
        doc.xref_set_key(parent, "Resources", resource)
        doc.xref_set_key(page.xref, "Resources", "null")
        snapshot = snapshot_default_spaces(page)
        self.assertEqual(len(snapshot['page_spaces']), 3)
        # The cleaned page can have fresh resources independent of its ancestor.
        doc.xref_set_key(page.xref, "Resources", "<< /ColorSpace << /NewEntry /DeviceGray >> >>")
        restore_default_spaces(page, snapshot)
        for name, value in snapshot['page_spaces'].items():
            self.assertEqual(doc.xref_get_key(page.xref, 'Resources/ColorSpace/' + name)[1], value)
        self.assertEqual(doc.xref_get_key(page.xref, "Resources/ColorSpace/NewEntry")[1], "/DeviceGray")
        doc.close()

    def test_geometry_match_does_not_override_different_image(self):
        doc, page, _ = fixture()
        snapshot = snapshot_default_spaces(page)
        form = snapshot['forms'][0]['xref']
        set_resource(doc, form, "ColorSpace", "<< /Other /DeviceRGB >>")
        image = page.get_images(full=True)[0][0]
        doc.update_stream(image, bytes([64]) * 300)
        self.assertEqual(restore_default_spaces(page, snapshot), 0)
        self.assertEqual(doc.xref_get_key(form, "Resources/ColorSpace/DefaultRGB")[0], "null")
        doc.close()


if __name__ == "__main__":
    unittest.main()
