"""Offline V4 parser/classification/layout regression tests with synthetic PDFs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.pdf_reader import parse, records_from_pages, annotate_visible_geometry
from core.classifier import classify
from core.layout import compile_layout, LayoutError, scientific_markup


def line(text, box, size=10, spans=None):
    return {"bbox": box, "dir": [1, 0], "spans": spans or [{"text": text, "bbox": box, "size": size, "font": "Helvetica", "flags": 0}]}


def block(text, box, size=10, lines=None):
    return {"type": 0, "bbox": box, "lines": lines or [line(text, box, size)]}


def inspection(blocks, tables=None):
    return {"sha256": "a"*64, "page_count": 1, "pages": [{"source_page": 1, "rect": [0, 0, 600, 800],
             "blocks": blocks, "drawing_clusters": [], "tables": tables or []}], "issues": []}


def merged(data):
    data = classify(data)
    data["layout_reviewed"] = True
    for b in data["blocks"]:
        if b["translation_required"]:
            b["translation"] = "完整译文 "+b["source_text"]
    return data


class ParserLayoutTests(unittest.TestCase):
    def test_wrapped_prose_does_not_enclose_native_image(self):
        lines=[line('Abstract. Prose alongside the graphic.',[40,100,280,120]),
               line('Another narrow line beside the graphic.',[40,125,280,145]),
               line('Full width prose below the original graphic.',[40,250,550,275])]
        out=merged(inspection([{'type':1,'bbox':[310,100,550,230]},block('',[40,100,550,275],lines=lines)]))
        self.assertEqual(len([b for b in out['blocks'] if b['translation_required']]),2)
        self.assertEqual(len(compile_layout(out)['pages'][0]['regions']),2)

    def test_inline_then_equation_is_prose_and_uppercase_heading_is_prose(self):
        out=classify(inspection([block('Delta G = 7 kJ. Then, K =',[40,100,280,120]),
                                block('PREDICTINGSOLUBILITYPRODUCTFROM',[40,200,280,215]),
                                block('SOLUBILITY',[40,218,280,233])]))
        self.assertTrue(all(b['translation_required'] for b in out['blocks']))

    def test_detected_table_header_closing_rule_protects_body(self):
        data=inspection([block('Table 1. Models.',[40,100,280,118]),
                         block('Model Species Interaction',[40,130,280,145]),
                         block('Basic Free ions Ionic strength',[40,150,280,190])],tables=[{'bbox':[40,130,280,145]}])
        data['pages'][0]['drawing_rules']=[[40,199,280,200]]
        out=classify(data);body=next(b for b in out['blocks'] if b['source_text'].startswith('Basic'))
        self.assertFalse(body['translation_required']);self.assertEqual(body['type'],'table')

    def test_floating_caption_inside_bibliography_is_translatable(self):
        data=inspection([block('References',[40,100,280,120]),
                         block('[1] A. Author. Study 2020.',[40,140,280,160]),
                         {'type':1,'bbox':[40,200,280,300]},
                         block('Figure 2. Measured performance.',[40,305,280,325]),
                         block('[2] B. Author. Study 2021.',[40,400,280,420])])
        out=classify(data);caption=next(b for b in out['blocks'] if b['source_text'].startswith('Figure'))
        self.assertEqual(caption['type'],'caption');self.assertTrue(caption['translation_required'])
        self.assertEqual([r['number'] for r in out['references']],[1,2])
        self.assertNotIn('Figure',out['references'][0]['text'])

    def test_rotated_margin_cannot_be_title(self):
        rotated=line('arXiv preprint',[10,100,30,500],20);rotated['dir']=[0,-1]
        data=inspection([block('',[10,100,30,500],lines=[rotated]),
                         block('A moderately enlarged paper title',[40,150,550,180],13.3),
                         block('Abstract. A complete abstract paragraph.',[40,230,550,260],10),
                         block('The paragraph contains normal prose text.',[40,300,550,330],10)])
        out=classify(data);self.assertEqual([b['source_text'] for b in out['blocks'] if b['type']=='title'],['A moderately enlarged paper title'])
    def test_real_pdf_parse_legacy_and_unified_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"sample.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((50, 60), "Synthetic paper title", fontsize=18)
            page.insert_text((50, 120), "A paragraph with native source geometry.", fontsize=10)
            page = doc.new_page(width=600, height=800)
            doc.save(path)
            doc.close()
            data = parse(path, Path(tmp)/"render")
            self.assertEqual(data["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(data["page_count"], 2)
            self.assertTrue(data["pages"][0]["blocks"][0]["lines"])
            self.assertEqual({r["id"] for r in data["records"]}, set(data["id_to_original"]))
            self.assertTrue((Path(tmp)/"render/page-001.png").is_file())
            self.assertIn("no-extractable-text", {i["check"] for i in data["issues"]})

    def test_id_invariance_insert_filter_reorder(self):
        a = block("Original paragraph", [40, 100, 280, 120])
        b = block("Other paragraph", [40, 150, 280, 170])
        image = {"type": 1, "bbox": [320, 100, 560, 250], "image_sha256": "abc"}
        original = records_from_pages(inspection([a, b])["pages"])[0]
        changed = records_from_pages(inspection([image, b, a])["pages"])[0]
        filtered = records_from_pages(inspection([b])["pages"])[0]
        self.assertEqual({r["text"]: r["id"] for r in original}, {r["text"]: r["id"] for r in changed if r["text"]})
        self.assertEqual(filtered[0]["id"], original[1]["id"])
        cmap = classify(inspection([image, b, a]))
        self.assertEqual({r["id"] for r in original}, set(cmap["expected_block_ids"]))

    def test_two_columns_order_and_blank_spacing(self):
        data = inspection([block("Right first paragraph has several words.", [320, 100, 560, 130]),
                           block("Left second paragraph has several words.", [40, 170, 280, 190]),
                           block("Left first paragraph has several words.", [40, 100, 280, 130]),
                           block("Right second paragraph has several words.", [320, 170, 560, 190])])
        mapping = merged(data)
        self.assertEqual([b["source_text"].split()[:2] for b in mapping["blocks"]],
                         [["Left", "first"], ["Left", "second"], ["Right", "first"], ["Right", "second"]])
        out = compile_layout(mapping)
        for region in out["pages"][0]["regions"]:
            self.assertEqual(region["rect"][:3], region["source_rects"][0][:3])
            if "first" in region["html"]:
                self.assertEqual(region["rect"][3], 169)
        self.assertEqual(out["pages"][0]["redactions"], [b["rect"] for b in mapping["blocks"]])

    def test_caption_format_and_geometry_false_positive(self):
        data = inspection([{ "type": 1, "bbox": [40, 100, 280, 230]},
                           block("Fig. 5 shows the measured value in this experiment.", [40, 240, 280, 260]),
                           block("Fig. 5. Measured performance.", [40, 270, 280, 290]),
                           block("Table 9. Remote caption candidate.", [320, 600, 560, 620])])
        out = classify(data)
        self.assertEqual(out["blocks"][0]["type"], "paragraph")
        self.assertEqual(out["blocks"][1]["type"], "caption")
        self.assertIn("fig/5", out["targets"])
        self.assertNotIn("table/9", out["targets"])
        self.assertTrue(out["need_review"])

    def test_display_formula_and_inline_fragments(self):
        data = inspection([block("The pressure and temperature were measured here.", [40, 100, 280, 120]),
                           block("2", [90, 98, 94, 108], 6),
                           block("x = y + z\n(2)", [40, 150, 280, 190], lines=[line("x = y + z", [40, 150, 260, 170]), line("(2)", [265, 172, 280, 190])])])
        out = merged(data)
        fragment = next(b for b in out["blocks"] if b["source_text"] == "2")
        self.assertTrue(fragment["translation_required"])
        formula = next(b for b in out["blocks"] if "x =" in b["source_text"])
        self.assertEqual(formula["type"], "formula")
        self.assertFalse(formula["translation_required"])
        self.assertIn("eq/2", out["targets"])
        layout = compile_layout(out)
        ids = [i for r in layout["pages"][0]["regions"] for i in r["source_block_ids"]]
        self.assertEqual(set(ids), {b["id"] for b in out["blocks"] if b["translation_required"]})
        self.assertTrue(any(p["id"] == formula["id"] for p in layout["pages"][0]["protected"]))

    def test_mixed_table_prose_split_by_lines(self):
        lines = [line("The following values summarize our measurements.", [40, 100, 280, 120]),
                 line("1.0 2.0 3.0", [40, 140, 280, 160]),
                 line("The next paragraph explains the measurement protocol.", [40, 200, 280, 220])]
        data = inspection([block("", [40, 100, 280, 220], lines=lines)], tables=[{"bbox": [35, 130, 285, 180]}])
        out = merged(data)
        self.assertEqual([b["type"] for b in out["blocks"]], ["paragraph", "table", "paragraph"])
        self.assertEqual(len(set(out["expected_block_ids"])), 3)
        self.assertTrue(all(b["original_ids"] == out["blocks"][0]["original_ids"] for b in out["blocks"]))
        layout = compile_layout(out)
        self.assertEqual(len(layout["pages"][0]["regions"]), 2)
        self.assertEqual(layout["pages"][0]["regions"][0]["rect"][3], 129)

    def test_leading_equation_number_and_spacing_rule(self):
        data = inspection([block("A paragraph stops before the native rule.", [40, 100, 280, 120]),
                           block("(4) E = mc2", [40, 200, 280, 220])])
        data["pages"][0]["drawing_clusters"] = [[40, 145, 280, 146]]
        out = merged(data)
        self.assertIn("eq/4", out["targets"])
        layout = compile_layout(out)
        self.assertEqual(layout["pages"][0]["regions"][0]["rect"][3], 144)

    def test_inline_micro_icon_does_not_truncate_its_text_block(self):
        # ORCID-style badges and other small icons sit inside an affiliation
        # block; they must not limit the block's downward growth.
        data = inspection([block("This paragraph describes the experimental method in enough detail.",
                                 [40, 100, 280, 176])])
        data["pages"][0]["spacing_obstacles"] = [[120, 150, 126, 156]]
        layout = compile_layout(merged(data))
        self.assertGreater(layout["pages"][0]["regions"][0]["rect"][3], 176)

    def test_navigation_bibliography_and_unavailable_supplement(self):
        data = inspection([block("Measurements follow [1,2] and Fig. 1. Supplementary Fig. S2 is unavailable.", [40, 100, 560, 120]),
                           {"type": 1, "bbox": [40, 160, 280, 260]},
                           block("Fig. 1. Recorded performance.", [40, 265, 280, 285]),
                           block("References", [40, 400, 280, 420]),
                           block("[1] A. Author. Original bibliography 2020.\n[2] B. Author. Other bibliography 2021.", [40, 450, 280, 490], lines=[line("[1] A. Author. Original bibliography 2020.", [40, 450, 280, 470]), line("[2] B. Author. Other bibliography 2021.", [40, 475, 280, 490])])])
        out = merged(data)
        self.assertEqual([r["number"] for r in out["references"]], [1, 2])
        self.assertTrue(any(b["inline_links"] for b in out["blocks"] if "Measurements" in b["source_text"]))
        layout = compile_layout(out)
        markup = next(r["html"] for r in layout["pages"][0]["regions"] if "Measurements" in r["html"])
        self.assertIn('https://pdf-reader.invalid/ref/1', markup)
        self.assertIn('https://pdf-reader.invalid/ref/2', markup)
        self.assertNotIn('fig/S2', layout["targets"])
        self.assertNotIn('invalid/fig/S2', markup)
        self.assertEqual(layout["references"], [])
        self.assertEqual(len(layout["bibliography"]), 2)
        self.assertEqual(out["targets"]["ref/1"]["point"], [40, 450])
        self.assertEqual(out["targets"]["ref/2"]["point"], [40, 475])
        out["explicitappendrefs"] = True
        self.assertEqual(len(compile_layout(out)["references"]), 2)

    def test_bibliography_tail_then_post_section(self):
        data = inspection([block("References", [40, 100, 280, 120]),
                           block("[5] A. Author. Bibliographic title and", [40, 150, 280, 170]),
                           block("journal continuation 2020, 12, 34-56. doi:10.0000/synthetic", [40, 180, 280, 200]),
                           block("Declaration of competing interest", [40, 250, 280, 270]),
                           block("The authors declare that no conflicts exist.", [40, 280, 280, 300])])
        out = classify(data)
        tail = next(b for b in out["blocks"] if b["source_text"].startswith("journal"))
        self.assertFalse(tail["translation_required"])
        self.assertEqual(tail["reference_continuation_of"], 5)
        self.assertIn("doi:10.0000", out["references"][0]["text"])
        self.assertTrue(next(b for b in out["blocks"] if b["source_text"].startswith("Declaration"))["translation_required"])
        self.assertTrue(out["blocks"][-1]["translation_required"])

    def test_newline_table_caption_and_greek_prose(self):
        data = inspection([block("Table 1\nα phase measurements", [40, 100, 280, 135],
                                 lines=[line("Table 1", [40, 100, 280, 115]), line("α phase measurements", [40, 120, 280, 135])]),
                           block("α phase", [320, 100, 560, 120]),
                           block("β kinetics", [320, 140, 560, 160]),
                           block("α = β + γ (3)", [320, 200, 560, 220])],
                          tables=[{"bbox": [40, 145, 280, 245]}])
        out = classify(data)
        self.assertEqual(next(b for b in out["blocks"] if b["source_text"].startswith("Table"))["type"], "caption")
        self.assertIn("table/1", out["targets"])
        self.assertTrue(next(b for b in out["blocks"] if b["source_text"] == "α phase")["translation_required"])
        self.assertTrue(next(b for b in out["blocks"] if b["source_text"] == "β kinetics")["translation_required"])
        self.assertEqual(next(b for b in out["blocks"] if "=" in b["source_text"])["type"], "formula")

    def test_bullet_heading_embedded_block_and_bare_numbers(self):
        data = inspection([block("Funding statement\n■ R E F E R E N C E S", [40, 100, 280, 140],
                                 lines=[line("Funding statement", [40, 100, 280, 115]), line("■ R E F E R E N C E S", [40, 125, 280, 140])]),
                           block("1.", [40, 160, 55, 175]),
                           block("A. Author. Original first bibliography entry 2020.", [60, 160, 280, 190]),
                           block("2.", [40, 205, 55, 220]),
                           block("B. Author. Original second bibliography entry 2021.", [60, 205, 280, 240])])
        out = classify(data)
        self.assertEqual([r["number"] for r in out["references"]], [1, 2])
        self.assertEqual(out["targets"]["ref/2"]["point"], [40, 205])
        self.assertIn("second bibliography", out["references"][1]["text"])

    def test_parenthesis_style_rejects_numeric_journal_tail(self):
        data = inspection([block("■REFERENCES", [40, 100, 280, 120]),
                           block("(1) A. Author. Native entry", [40, 140, 280, 160]),
                           block("379.", [40, 170, 60, 185]),
                           block("(2) B. Author. Second entry", [40, 200, 280, 220])])
        out = classify(data)
        self.assertEqual([r["number"] for r in out["references"]], [1, 2])
        self.assertIn("379.", out["references"][0]["text"])

    def test_masthead_and_spaced_abstract(self):
        data = inspection([block("Example Publication", [200, 40, 400, 60], 16),
                           block("A sufficiently descriptive scientific article title", [40, 130, 560, 155], 15),
                           block("A B S T R A C T", [40, 230, 280, 250]),
                           block("This is the first abstract paragraph with several words.", [40, 260, 280, 300])])
        out = classify(data)
        self.assertEqual(len([b for b in out["blocks"] if b["type"] == "title"]), 1)
        self.assertEqual(next(b for b in out["blocks"] if b["source_text"] == "Example Publication")["role"], "publication-masthead")
        self.assertEqual(next(b for b in out["blocks"] if b["source_text"] == "A B S T R A C T")["type"], "abstract")

    def test_narrow_gutter_number_fragment_does_not_reorder_references(self):
        data = inspection([block("5. Conclusions", [40, 610, 100, 625]),
                           block("The left column concludes the complete article.", [40, 630, 299, 660]),
                           block("References", [306, 600, 360, 615]),
                           block("[1] A. Author. Bibliography", [306, 630, 565, 660]),
                           block("2", [296, 500, 301, 510]),
                           block("Body paragraph on the right side above references.", [306, 500, 565, 530])])
        out = classify(data)
        self.assertEqual([r["number"] for r in out["references"]], [1])
        self.assertTrue(next(b for b in out["blocks"] if b["source_text"] == "5. Conclusions")["translation_required"])

    def test_optional_real_inspection_reference_structure(self):
        try:
            root = Path(__file__).resolve().parents[3]/"v4-upgrade"
        except IndexError:
            self.skipTest("External real-paper inspections are not part of the portable synthetic corpus")
        if not all((root/(name+"-auto")/"inspection.json").exists() for name in ("han", "monat", "pan")):
            self.skipTest("External real-paper inspections are not part of the portable synthetic corpus")
        for name, count in (("han", 42), ("monat", 8), ("pan", 75)):
            with self.subTest(sample=name):
                out = classify(json.loads((root/(name+"-auto")/"inspection.json").read_text(encoding="utf-8")))
                self.assertEqual([r["number"] for r in out["references"]], list(range(1, count+1)))
                self.assertEqual(len([k for k in out["targets"] if k.startswith("ref/")]), count)
                if name == "han":
                    self.assertEqual(len([b for b in out["blocks"] if b["type"] == "title"]), 1)
                    self.assertEqual(next(b for b in out["blocks"] if b["source_text"] == "A B S T R A C T")["type"], "abstract")

    def test_short_english_fragments_and_chemical_arrow(self):
        data = inspection([block("of", [40, 100, 48, 115]), block("is", [60, 100, 67, 115]),
                           block("A R T I C L E I N F O", [40, 50, 140, 65]),
                           block("XY + NaCl → NaY + XCl", [40, 180, 280, 195])])
        out = classify(data)
        self.assertTrue(all(b["translation_required"] for b in out["blocks"] if b["source_text"] in {"of", "is"}))
        self.assertFalse(next(b for b in out["blocks"] if "→" in b["source_text"])["translation_required"])
        self.assertEqual(next(b for b in out["blocks"] if "A R T" in b["source_text"])["role"], "metadata")

    def test_separate_three_rule_tables_and_external_axis(self):
        data = inspection([block("Table 1\nFirst native table.", [40, 60, 280, 90], lines=[line("Table 1", [40, 60, 280, 72]), line("First native table.", [40, 76, 280, 90])]),
                           block("Category", [45, 100, 90, 112]), block("Value", [180, 100, 220, 112]),
                           block("1.25", [180, 125, 220, 137]),
                           block("The paragraph between tables remains fully translatable.", [40, 200, 280, 230]),
                           block("Table 2\nSecond native table.", [40, 300, 280, 330], lines=[line("Table 2", [40, 300, 280, 312]), line("Second native table.", [40, 316, 280, 330])]),
                           block("Category", [45, 340, 90, 352]), block("1.25", [180, 365, 220, 377]),
                           block("Time (min)", [360, 260, 420, 272])])
        data["pages"][0]["drawing_rules"] = [[40,y,280,y+.5] for y in [95,120,145,335,360,385]]
        data["pages"][0]["drawing_clusters"] = [[320,100,550,240]]
        out = classify(data)
        self.assertEqual(len([p for p in out["pages"][0]["protected"] if p["type"] == "table"]), 2)
        self.assertTrue(next(b for b in out["blocks"] if b["source_text"].startswith("The paragraph"))["translation_required"])
        self.assertTrue(all(not b["translation_required"] for b in out["blocks"] if b["source_text"] == "Category"))
        self.assertFalse(next(b for b in out["blocks"] if b["source_text"] == "Time (min)")["translation_required"])

    def test_source_scientific_markup_keeps_citation_plain(self):
        lines = [{"spans": [{"text":"Ca", "size":7.97,"origin":[100,579.287]},
                             {"text":"2+", "size":5.635,"origin":[109,575.773]}]},
                 {"spans": [{"text":"SO", "size":7.97,"origin":[100,589.72]},
                             {"text":"4", "size":5.635,"origin":[112,591.08]}]},
                 {"spans": [{"text":"reported", "size":7.97,"origin":[100,600]},
                             {"text":"2", "size":5.635,"origin":[140,596]}]}]
        text = "Ca2+ and SO4 were reported2."
        rendered = scientific_markup(text, lines)
        self.assertEqual(rendered, "Ca<sup>2+</sup> and SO<sub>4</sub> were reported2.")
        self.assertEqual(scientific_markup(rendered, lines), rendered)
        import re
        self.assertEqual(re.sub(r"<[^>]+>", "", rendered), text)

    def test_body_inline_fragments_precede_expanded_figure_geometry(self):
        text = "The paragraph describes measurements and includes scientific quantities without omitting any important words or values."
        data = inspection([block(text, [40, 100, 280, 140], lines=[line(text, [40, 100, 280, 120]), line("The final line continues here", [40, 125, 175, 140])]),
                           block("1.45–2.95 cm/s", [180, 125, 230, 140]),
                           block("are not", [235, 125, 280, 140]), block("[37].", [270, 105, 280, 120]),
                           block("Time (min)", [110, 300, 180, 312])])
        data["pages"][0]["drawing_clusters"] = [[75,170,280,285]]
        out = merged(data)
        self.assertTrue(all(b["translation_required"] for b in out["blocks"] if b["source_text"] in {"1.45–2.95 cm/s", "are not", "[37]."}))
        self.assertFalse(next(b for b in out["blocks"] if b["source_text"] == "Time (min)")["translation_required"])
        compile_layout(out)

    def test_multiline_graph_ticks_do_not_use_whole_record_baseline(self):
        text = "The paragraph remains readable and describes the experiment with enough actual English words to be prose."
        data = inspection([block(text, [40, 190, 270, 210]),
                           block("0\n10\n20\n30", [280, 150, 290, 240], lines=[line(str(n), [280,150+i*25,290,165+i*25]) for i,n in enumerate([0,10,20,30])])])
        data["pages"][0]["drawing_clusters"] = [[300,140,550,245]]
        out = classify(data)
        ticks = [b for b in out["blocks"] if b["source_text"].splitlines()[0] == "0"]
        self.assertTrue(ticks)
        self.assertTrue(all(not b["translation_required"] for b in ticks))

    def test_full_width_caption_below_multiple_panel_labels(self):
        data = inspection([block("Fig. 6. Native panels with a combined caption.", [40, 340, 560, 355]),
                           block("Fig. 6 shows the ordinary prose discussion.", [40, 370, 560, 390])])
        data["pages"][0]["drawing_clusters"] = [[70,100,280,298],[320,100,540,299]]
        out = classify(data)
        self.assertEqual(out["blocks"][0]["type"], "caption")
        self.assertEqual(out["blocks"][1]["type"], "paragraph")
        self.assertIn("fig/6", out["targets"])

    def test_oversized_blank_span_tight_geometry_keeps_math(self):
        text = "The native paragraph has complete source coverage."
        oversized = [40, 50, 280, 240]
        body_line = line(text+" ", oversized, spans=[
            {"text": text, "bbox": [40, 100, 250, 120], "size": 10, "font": "Helvetica"},
            {"text": " ", "bbox": [250, 50, 280, 240], "size": 10, "font": "LMRoman"}])
        blocks = [block("", oversized, lines=[body_line]), block("x = y (1)", [40, 150, 280, 180])]
        raw_body = {"type": 0, "lines": [{"spans": [
            {"chars": [{"c": c, "bbox": [40+i*4, 100, 44+i*4, 120]} for i, c in enumerate(text)]},
            {"chars": [{"c": " ", "bbox": [250, 50, 280, 240]}]}]}]}
        raw_math = {"type": 0, "lines": [{"spans": [{"chars": [
            {"c": c, "bbox": [40+i*8, 150 if c != 'y' else 145, 48+i*8, 180]} for i, c in enumerate("x = y (1)")]}]}]}
        annotate_visible_geometry(blocks, [raw_body, raw_math])
        self.assertEqual(blocks[0]["bbox"], oversized)
        self.assertEqual(blocks[0]["nonwhitespace_bbox"][1:4:2], [100, 120])
        self.assertEqual(blocks[1]["nonwhitespace_bbox"][1], 145)
        out = merged(inspection(blocks))
        prose = next(b for b in out["blocks"] if b["translation_required"])
        self.assertEqual(prose["original_bbox"], oversized)
        self.assertEqual(prose["rect"][1:4:2], [100, 120])
        layout = compile_layout(out)
        self.assertEqual(layout["pages"][0]["regions"][0]["rect"][3], 144)

    @unittest.skipUnless(Path("C:/Windows/Fonts/msyh.ttc").exists(), "Windows Chinese font needed for legacy writer")
    def test_native_references_do_not_append_writer_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, output = tmp/"source.pdf", tmp/"out.pdf"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((40, 100), "References", fontsize=10)
            page.insert_text((40, 150), "[1] A. Author. Original bibliography 2020.", fontsize=10)
            doc.save(source)
            doc.close()
            mapping = classify(parse(source))
            mapping["layout_reviewed"] = True
            for b in mapping["blocks"]:
                b.update(translation_required=False, preservation_reason="native-reference-test")
            manifest = compile_layout(mapping)
            manifest_path = tmp/"manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            run = subprocess.run([sys.executable, "scripts/pdf_reader.py", "build", "--source", str(source),
                                  "--manifest", str(manifest_path), "--output", str(output), "--font-dir", "C:/Windows/Fonts"],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
            with fitz.open(output) as rebuilt:
                self.assertEqual(len(rebuilt), 1)

    def test_native_overlap_and_unreviewed_map_rejected(self):
        data = inspection([block("This paragraph overlaps an image and needs review.", [40, 100, 280, 140]),
                           {"type": 1, "bbox": [200, 130, 400, 250]}])
        out = classify(data)
        with self.assertRaises(LayoutError) as ctx:
            compile_layout(out)
        self.assertEqual(ctx.exception.issues[0]["check"], "source-map-not-reviewed")
        out = merged(data)
        with self.assertRaises(LayoutError) as ctx:
            compile_layout(out)
        self.assertIn("protected-intersection", {i["check"] for i in ctx.exception.issues})

    def test_single_column_native_units_and_author_metadata(self):
        data = inspection([block("Synthetic Paper Title", [40, 30, 560, 55], 20),
                           block("A Author, B Researcher", [40, 60, 560, 80]),
                           block("Department of Science, Example University", [40, 85, 560, 105]),
                           block("Abstract: The values were obtained at 25 degrees Celsius.", [40, 130, 560, 170]),
                           block("25 °C", [40, 200, 560, 220])])
        out = classify(data)
        metadata = [b for b in out["blocks"] if b.get("role") == "metadata"]
        self.assertEqual(len(metadata), 2)
        self.assertTrue(all(not b["translation_required"] for b in metadata))
        self.assertEqual(next(b for b in out["blocks"] if b["source_text"].startswith("Abstract"))["type"], "abstract")
        self.assertEqual({b["column"] for b in out["blocks"]}, {"single"})
        self.assertEqual(next(b for b in out["blocks"] if b["source_text"] == "25 °C")["preservation_reason"], "native-unit")


if __name__ == "__main__":
    unittest.main()
