"""Upstream filters for false protected figures and ACS author headings.

Drawing-cluster promotion happens before ``pages[].protected`` is written.
These cases are the EST 2023 Pärnamäe geometry: a wide frame over the abstract,
a swapped zero-height image box, and Authors / Corresponding Authors frozen as
formulas because a neighbouring affiliation line contains U+2212.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.classifier import author_section_heading, classify
from core.pdf_reader import records_from_pages, valid_bbox
from core.structure import preflight
from test_parser_layout import block


ABSTRACT = (
    "ABSTRACT: Bipolar membranes are a special class of ion exchange membranes "
    "used for water dissociation and electrochemical separations in this study."
)
PROSE = (
    "The following paragraph explains the measured current and voltage response "
    "of the membrane in enough ordinary English words to count as long prose."
)
PAGE = [0, 0, 600, 800]


def _page(blocks, clusters=None, tables=None, number=1):
    return {"source_page": number, "rect": list(PAGE), "blocks": blocks,
            "drawing_clusters": clusters or [], "tables": tables or []}


def _inspection(pages):
    return {"sha256": "a" * 64, "page_count": len(pages), "pages": pages, "issues": []}


def _protected(out, page=1):
    return [item for item in out["pages"][page - 1]["protected"]]


def _has_rect(protected, rect):
    return any(all(math.isclose(a, b, abs_tol=0.01) for a, b in zip(item["rect"], rect))
               for item in protected)


class DrawingClusterPromotionTests(unittest.TestCase):
    def test_wide_cluster_over_abstract_is_not_protected_but_real_figures_are(self):
        # The abstract box sticks out below the frame, so full containment does
        # not see it. Overlap is still most of the smaller area.
        frame = [1.5, 220, 590, 460]
        abstract_box = [50, 235, 300, 500]
        # Not a full-width band: only the overlap rule applies, and the prose
        # box is not contained by the cluster.
        partial = [30, 180, 290, 400]
        partial_prose = [40, 200, 280, 520]
        # Wide and short, and it does not touch the prose. The band rule alone
        # must refuse it.
        band = [5, 40, 580, 100]
        figure = [400, 500, 560, 740]
        # Grazes the second prose block by well under 15% of the smaller area.
        grazing = [400, 180, 560, 360]
        prose_box = [300, 340, 430, 400]
        data = _inspection([_page([
            block(ABSTRACT, abstract_box),
            block(PROSE, prose_box),
            block(PROSE, partial_prose),
            {"type": 1, "bbox": [420, 520, 540, 700], "image_sha256": "real"},
        ], clusters=[frame, partial, band, figure, grazing])])
        out = classify(data)
        protected = _protected(out)
        for refused in (frame, partial, band):
            self.assertFalse(_has_rect(protected, refused), refused)
        self.assertTrue(_has_rect(protected, figure))
        self.assertTrue(_has_rect(protected, grazing))
        self.assertTrue(_has_rect(protected, [420, 520, 540, 700]))
        kinds = {tuple(round(v, 1) for v in item["rect"]): item["type"] for item in protected}
        self.assertEqual(kinds[tuple(figure)], "figure")

    def test_overlap_threshold_is_strictly_above_fifteen_percent(self):
        kept = [0, 100, 200, 200]
        dropped = [0, 100, 200, 200]
        # Equal areas. A 15pt overlap is 15% of the smaller box; 16pt is over.
        on_threshold = [0, 185, 200, 285]
        over_threshold = [0, 184, 200, 284]
        kept_out = classify(_inspection([_page([block(PROSE, on_threshold)], clusters=[kept])]))
        dropped_out = classify(_inspection([_page([block(PROSE, over_threshold)], clusters=[dropped])]))
        self.assertTrue(_has_rect(_protected(kept_out), kept))
        self.assertFalse(_has_rect(_protected(dropped_out), dropped))

    def test_caption_overlap_blocks_promotion(self):
        caption = (
            "Figure 1. Measured limiting current for the bipolar membrane "
            "across the full range of salt concentrations studied here."
        )
        cluster = [40, 80, 280, 250]
        # Caption starts inside the cluster and continues below it.
        caption_box = [40, 200, 280, 320]
        out = classify(_inspection([_page([block(caption, caption_box)], clusters=[cluster])]))
        self.assertFalse(_has_rect(_protected(out), cluster))


class InvalidProtectedRectTests(unittest.TestCase):
    def test_swapped_and_empty_image_rects_are_not_written(self):
        swapped = [485.999, 271.106, 67.691, 271.106]
        flat = [10, 10, 40, 10]
        broken = [0, 0, float("nan"), 20]
        good = [40, 60, 200, 180]
        pages = [_page([
            block("A short ordinary sentence on the page.", [40, 400, 280, 430]),
            {"type": 1, "bbox": swapped, "image_sha256": "swapped"},
            {"type": 1, "bbox": flat, "image_sha256": "flat"},
            {"type": 1, "bbox": broken, "image_sha256": "nan"},
            {"type": 1, "bbox": good, "image_sha256": "good"},
        ], clusters=[[20, 20, 10, 80]])]
        records, _, _ = records_from_pages(pages)
        self.assertTrue(all(valid_bbox(record["bbox"]) for record in records))
        self.assertFalse(any(record["bbox"] == swapped for record in records))
        data = _inspection(pages)
        data["records"] = records
        out = classify(data)
        protected = _protected(out)
        self.assertTrue(all(valid_bbox(item["rect"]) for item in protected))
        self.assertTrue(_has_rect(protected, good))
        self.assertFalse(any(item["rect"][2] <= item["rect"][0] or item["rect"][3] <= item["rect"][1]
                             for item in protected))
        report = preflight(out, data)
        checks = {issue["check"] for issue in report["issues"]}
        self.assertNotIn("invalid-protected-rectangle", checks)
        self.assertNotIn("unprotected-source-image", checks)


class AuthorSectionHeadingTests(unittest.TestCase):
    def test_label_helper_accepts_acs_variants_only(self):
        for label in ("Authors", "Author", "Corresponding Authors", "Corresponding Author",
                      "Authors:", "*Corresponding Authors", "Co-corresponding Authors",
                      "Corresponding author(s)"):
            self.assertTrue(author_section_heading(label), label)
        for sentence in ("The authors declare no competing financial interest.",
                         "Author Contributions", "AUTHOR INFORMATION",
                         "Michele Tedesco −Wetsus, European Centre of Excellence for"):
            self.assertFalse(author_section_heading(sentence), sentence)

    def test_authors_labels_translate_as_headers_beside_minus_affiliations(self):
        # Page 9 of an ACS paper, not page 1: the first-page metadata rule must
        # not be what keeps these labels out of the formula bucket.
        affiliation = "Michele Tedesco −Wetsus, European Centre of Excellence for"
        other = "Ragne Pärnamäe −Wetsus, European Centre of Excellence for"
        pages = [
            _page([block("A sufficiently descriptive scientific article title about membranes.",
                         [40, 120, 560, 150], 16)], number=1),
            _page([
                block("Authors", [315.44, 59.32, 348.36, 68.82]),
                block(affiliation, [325.5, 71.2, 555.4, 81.9]),
                block("Corresponding Authors", [51.48, 611.34, 149.31, 620.84]),
                block(other, [61.5, 623.4, 291.5, 634.2]),
                block("The authors declare no competing financial interest.", [315.4, 396.5, 518.8, 420]),
                block("Author Contributions", [315.4, 430, 450, 445]),
                block("Notes", [315.4, 460, 360, 475]),
            ], number=2),
        ]
        out = classify(_inspection(pages))
        by_text = {b["source_text"]: b for b in out["blocks"] if b["source_page"] == 2}
        for label in ("Authors", "Corresponding Authors"):
            item = by_text[label]
            self.assertEqual(item["type"], "header", label)
            self.assertEqual(item.get("role"), "section-heading", label)
            self.assertTrue(item["translation_required"], label)
            self.assertFalse(item.get("preservation_reason"), label)
        prose = by_text["The authors declare no competing financial interest."]
        self.assertEqual(prose["type"], "paragraph")
        self.assertTrue(prose["translation_required"])
        contributions = by_text["Author Contributions"]
        self.assertTrue(contributions["translation_required"])
        self.assertNotEqual(contributions["type"], "formula")
        flagged = {issue["id"] for issue in out["issues"] if issue.get("check") == "formula-with-prose-candidate"}
        self.assertNotIn(by_text["Authors"]["id"], flagged)
        self.assertNotIn(by_text["Corresponding Authors"]["id"], flagged)


if __name__ == "__main__":
    unittest.main()
