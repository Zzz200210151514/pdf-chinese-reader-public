import re
import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.scientific_markup import restore_scientific_markup


def span(text, size=7.97, y=579.287, **extra):
    return dict(text=text, size=size, origin=[30, y], **extra)


def line(*spans, **extra):
    return dict(spans=list(spans), **extra)


class ScientificMarkupTests(unittest.TestCase):
    def test_greek_and_italic_variable_subscripts(self):
        lines=[line(span('κ'),span('0',5,581)),line(span('m',flags=2),span('R',5,581))]
        self.assertEqual(restore_scientific_markup('κ0 mR',lines),'κ<sub>0</sub> m<sub>R</sub>')
    def test_cut_leading_charge_has_same_line_baseline_evidence(self):
        lines=[line(span('2−',5,575),span(', HCO',8,579))]
        self.assertEqual(restore_scientific_markup('2−，HCO3',lines),'<sup>2−</sup>，HCO3')
    def test_native_charge_and_subscript_in_sentence(self):
        lines = [line(span("Dissolved Ca"), span("2+", 5.635, 575.773)),
                 line(span("and SO", y=589.72), span("4", 5.635, 591.08))]
        before = deepcopy(lines)
        text = "溶解的Ca2+与SO4。[12]"
        result = restore_scientific_markup(text, lines)
        self.assertEqual(result, "溶解的Ca<sup>2+</sup>与SO<sub>4</sub>。[12]")
        self.assertEqual(re.sub(r"</?(?:sub|sup)>", "", result), text)
        self.assertEqual(lines, before)

    def test_units_variables_and_split_charge_spans(self):
        lines = [line(span("dose mR"), span("−1", 5, 575)),
                 line(span("mass kg"), span("−1", 5, 575)),
                 line(span("Ca"), span("2", 5, 575), span("+", 5, 575)),
                 line(span("xQ"), span("2", 5, 581))]
        self.assertEqual(restore_scientific_markup("mR−1 kg−1 Ca2+ xQ2", lines),
                         "mR<sup>−1</sup> kg<sup>−1</sup> Ca<sup>2+</sup> xQ<sub>2</sub>")

    def test_normal_text_citations_and_unshifted_small_text(self):
        lines = [line(span("body"), span("12", 5, 575)),
                 line(span("of"), span("2", 5, 575)),
                 line(span("Ca"), span("12", 5, 575)),
                 line(span("SO"), span("4", 5, 579.3)),
                 line(span("small"), span("note", 5, 575))]
        text = "body12 plain of2 Ca12 SO4 smallnote [12]"
        self.assertEqual(restore_scientific_markup(text, lines), text)

    def test_existing_tags_formulas_and_longer_tokens_preserved(self):
        lines = [line(span("Ca"), span("2+", 5, 575)),
                 line(span("SO"), span("4", 5, 581))]
        text = 'Ca<sup>2+</sup> <sub>SO4</sub> $Ca2+$ \\(SO4\\) Ca2+ Ca2+9 ASO4'
        expected = 'Ca<sup>2+</sup> <sub>SO4</sub> $Ca2+$ \\(SO4\\) Ca<sup>2+</sup> Ca2+9 ASO4'
        result = restore_scientific_markup(text, lines)
        self.assertEqual(result, expected)
        self.assertEqual(restore_scientific_markup(result, lines), result)

    def test_cross_line_charge_and_configured_formula_unchanged(self):
        lines = [line(span("Ca")), line(span("2+", 5, 575)),
                 line(span("SO"), span("4", 5, 581), type="formula")]
        self.assertEqual(restore_scientific_markup("Ca\n2+ SO4", lines), "Ca\n2+ SO4")

    def test_both_subscript_and_charge(self):
        lines = [line(span("SO"), span("4", 5, 581), span("2−", 5, 575))]
        self.assertEqual(restore_scientific_markup("SO42−", lines), "SO<sub>4</sub><sup>2−</sup>")

    def test_missing_geometry_and_conflicting_baselines(self):
        lines = [line(dict(text="Ca", size=8), span("2+", 5, 575)),
                 line(span("SO"), span("4", 5, 581)),
                 line(span("SO"), span("4", 5, 575))]
        self.assertEqual(restore_scientific_markup("Ca2+", lines), "Ca2+")
        conflicting = [line(span("kg"), span("2", 5, 575)),
                       line(span("kg"), span("2", 5, 581))]
        self.assertEqual(restore_scientific_markup("kg2", conflicting), "kg2")

    def test_exact_characters_and_tag_attributes_preserved(self):
        lines = [line(span("Ca"), span("2+", 5, 575))]
        text = '<a title="Ca2+">Ca2+</a>；\nCa2+ &amp; Ca3+'
        result = restore_scientific_markup(text, lines)
        self.assertEqual(result, '<a title="Ca2+">Ca<sup>2+</sup></a>；\nCa<sup>2+</sup> &amp; Ca3+')
        self.assertEqual(re.sub(r"</?(?:sub|sup)>", "", result), text)


if __name__ == "__main__":
    unittest.main()
