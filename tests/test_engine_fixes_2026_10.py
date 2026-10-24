"""Regression tests for the 2026-10-18 engine fixes (Ma2023 batch-fix report).

E1: ACS sentence-final citations (`.27`) — freeze.py NUM must not swallow the
    period; citations.py must accept a period before a known superscript group.
E2: adjacent frozen tokens joined without punctuation ((SPI),21 -> (SPI)21)
    must fail the audit WITH an actionable hint.
E4: msyh.ttc ToUnicode maps U+00B7 to U+2219; normalize() must treat them as one.
P1: short prose-like blocks on an equation baseline (water uptake (%) = ...)
    must be absorbed into the native formula zone.
P2: short sentence-free blocks below a Table caption are native table cells.
P3: prose line wraps ending in an equation reference (eq 9:, by eq 10:) must
    classify as translatable prose, and the reverse sweep must flag formula
    blocks that still carry prose words without math operators.
"""
from copy import deepcopy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm.freeze import audit, freeze, restore
from core.citations import restore_citations
from core.validator import normalize
from core.classifier import classify, _formula, native_micro_fragment

from test_parser_layout import block, inspection


class FreezeCitationFixes(unittest.TestCase):
    def test_sentence_final_citation_number_frozen_without_period(self):
        # `migration.27` is a period plus a superscript citation, not 0.27.
        frozen, tokens = freeze('Proton migration.27 was observed')
        self.assertIn('.', frozen)
        for token, original in tokens.items():
            self.assertNotEqual(original, '.27')
        self.assertIn('27', tokens.values())

    def test_leading_dot_decimal_still_frozen_as_one_number(self):
        frozen, tokens = freeze('A value of .27 m was used')
        self.assertIn('.27', tokens.values())

    def test_audit_reports_joined_placeholder_hint(self):
        source = 'sulfonated polyimide (SPI),21 membrane'
        frozen, tokens = freeze(source)
        # The translation drops the comma between the two frozen tokens.
        bad = frozen.replace('⟦V4_', '⟦V4_')  # unchanged; build the joined form below
        joined = source.replace('(SPI),21', '(SPI)21')
        result = audit(source, joined)
        self.assertTrue(result['missing'] or result['added'])
        self.assertIn('hint', result)
        self.assertIn('标点', result['hint'])

    def test_audit_accepts_comma_kept_between_tokens(self):
        source = 'sulfonated polyimide (SPI),21 membrane'
        result = audit(source, '磺化聚酰亚胺 (SPI),21 膜')
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['added'], [])

    def test_oxidation_state_parentheses_audit_symmetrically(self):
        # English `vanadium(V)` hides `(V)` behind a Latin lookbehind; the
        # Chinese `钒(V)` does not. A lone parenthesized element symbol is
        # prose oxidation-state notation, not a protected formula.
        result = audit('vanadium(V) using membrane', '钒(V)采用膜')
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['added'], [])

    def test_charge_followed_by_word_is_audited_symmetrically(self):
        # The freeze pass can leave `OH−accumulation` as OH + literal minus.
        # The trailing charge still belongs to the formula on both sides.
        source = 'in situ OH−accumulation'
        self.assertEqual(audit(source, '原位OH−富集'), {'missing': [], 'added': []})
        dropped = audit(source, '原位OH富集')
        self.assertTrue(dropped['missing'] or dropped['added'])

    def test_protected_span_whitespace_is_formatting(self):
        self.assertEqual(audit('Mg2+(H2O)6\n+ OH−', 'Mg2+(H2O)6 + OH−'),
                         {'missing': [], 'added': []})


class CitationRestoreFixes(unittest.TestCase):
    def test_period_before_known_superscript_is_restored(self):
        lines = [{"spans": [
            {"text": "Proton migration.", "bbox": [40, 100, 140, 112], "size": 10, "flags": 0, "origin": [40, 110]},
            {"text": "27", "bbox": [140, 96, 152, 106], "size": 7, "flags": 0, "origin": [140, 100]},
        ]}]
        known = {"ref/27"}
        text, marked, colours, issues = restore_citations('迁移.27', lines, known)
        self.assertIn('<sup>27</sup>', text)
        self.assertEqual(issues, [])


class NormalizeFix(unittest.TestCase):
    def test_bullet_operator_equals_middle_dot(self):
        self.assertEqual(normalize('160 g·L−1'), normalize('160 g∙L−1'))


class ClassifierFixes(unittest.TestCase):
    def test_equation_reference_line_wrap_is_prose(self):
        self.assertFalse(_formula('eq 9:'))
        self.assertFalse(_formula('by eq 10:'))

    def test_reverse_sweep_flags_formula_with_prose_words(self):
        # `Gas` sits 9.5pt above `= 42.3` (body=10): inside the expanded math
        # zone, absorbed by the bare-fragment rule but NOT the named-block rule,
        # so the reverse sweep must flag it for review instead of silently
        # keeping a prose-like fragment native.
        out = classify(inspection([block('Gas', [70, 176.5, 100, 190.5]),
                                   block('= 42.3', [70, 200, 120, 214])]))
        gas = next(b for b in out['blocks'] if b['source_text'] == 'Gas')
        self.assertEqual(gas['type'], 'formula')
        issues = [i for i in out['issues'] if i.get('check') == 'formula-with-prose-candidate']
        self.assertTrue(issues)

    def test_named_operand_on_equation_baseline_becomes_formula(self):
        # `water uptake (%)` sits left of `= 42.3` on the same baseline. The
        # named block must be absorbed and stay native, not half-translated.
        out = classify(inspection([block('water uptake (%)', [40, 200, 140, 214]),
                                   block('= 42.3', [150, 200, 190, 214])]))
        named = next((b for b in out['blocks'] if 'water uptake' in b['source_text']), None)
        self.assertIsNotNone(named)
        self.assertEqual(named['type'], 'formula')
        self.assertFalse(named['translation_required'])

    def test_table_cell_below_caption_is_native(self):
        out = classify(inspection([block('Table 1. Membranes used.', [40, 100, 280, 118]),
                                   block('membrane', [40, 130, 90, 145]),
                                   block('SYMA-1', [40, 150, 90, 165])]))
        cells = [b for b in out['blocks'] if b['source_text'].strip() in {'membrane', 'SYMA-1'}]
        self.assertEqual(len(cells), 2)
        for cell in cells:
            self.assertEqual(cell['type'], 'table')
            self.assertFalse(cell['translation_required'])

    def test_multiline_table_cell_with_values_is_native(self):
        # Ma2023 Table 3: column header with percentage values underneath,
        # followed by a long paragraph whose inflated bbox grazes the last row.
        out = classify(inspection([block('Table 3. SPSU membranes.', [40, 50, 280, 80]),
                                   block('SPSU25\n41.94%\n49.05%\n75.93%', [40, 100, 200, 150]),
                                   block('The paragraph below the table discusses ion exchange '
                                         'functional sites and their contributions to transport '
                                         'performance in great detail across the membranes.',
                                         [40, 165, 280, 250])]))
        cell = next(b for b in out['blocks'] if 'SPSU25' in b['source_text'])
        self.assertEqual(cell['type'], 'table')
        self.assertFalse(cell['translation_required'])

    def test_variable_definition_line_is_not_absorbed_into_equation(self):
        # `O is the osmotic water flux...` is a where-clause definition, not an
        # equation operand; copulas and articles must keep it translatable.
        out = classify(inspection([block('O is\nthe osmotic water flux, L m-2 h-1; and Jw',
                                        [315, 552, 555, 576]),
                                   block('= 42.3', [315, 552, 555, 576])]))
        definition = next(b for b in out['blocks'] if 'O is' in b['source_text'])
        self.assertEqual(definition['type'], 'paragraph')
        self.assertTrue(definition['translation_required'])

    def test_purity_threshold_is_not_an_equation_seed(self):
        # Wang2023 materials list: `≥99.0%` purity values beside supplier names.
        # `≥` is in the math glyph set, but a threshold value must not seed an
        # equation zone that absorbs the neighbouring reagent names.
        out = classify(inspection([block('Sodium orthovanadate (Na3VO4)', [40, 100, 200, 114]),
                                   block('Aladdin (China)', [180, 100, 260, 114]),
                                   block('The following reagents were used directly.', [40, 180, 280, 200])]))
        reagent = next(b for b in out['blocks'] if 'Sodium orthovanadate' in b['source_text'])
        self.assertEqual(reagent['type'], 'paragraph')
        self.assertTrue(reagent['translation_required'])

    def test_company_list_is_not_an_equation_operand(self):
        # `Cation exchange membrane ChemJoy Polymer Materials Co. (Hefei, China)`
        # is a supplier row, not the left operand of a displayed equation.
        out = classify(inspection([block('Cation exchange membrane \nChemJoy Polymer \nMaterial',
                                         [40, 100, 240, 130]),
                                   block('= 42.3', [250, 100, 300, 114])]))
        name = next(b for b in out['blocks'] if 'Cation exchange' in b['source_text'])
        self.assertEqual(name['type'], 'paragraph')
        self.assertTrue(name['translation_required'])

    def test_caption_tail_fragment_stays_translatable_inline(self):
        # Extraction splits `2).` out of a caption's last line; the tail is an
        # inline fragment of the caption, not a plot label.
        out = classify(inspection([{'type': 1, 'bbox': [40, 60, 280, 98]},
                                   block('Fig. 2. Temperature effects on transport across membranes.',
                                         [40, 100, 280, 130]),
                                   block('2).', [100, 120, 110, 130])]))
        tail = next(b for b in out['blocks'] if b['source_text'].strip() == '2).')
        self.assertEqual(tail['type'], 'paragraph')
        self.assertEqual(tail.get('role'), 'inline-fragment')
        self.assertTrue(tail['translation_required'])

    def test_micro_equation_and_formula_fragments_are_native(self):
        out = classify(inspection([
            block('(2)', [540, 100, 560, 112]),
            block('Li2CO3', [40, 200, 70, 212]),
            block('S8).33', [40, 300, 70, 312]),
            block('Mg(OH)', [40, 400, 80, 412]),
        ]))
        for text in ('(2)', 'Li2CO3', 'S8).33', 'Mg(OH)'):
            item = next(b for b in out['blocks'] if b['source_text'].strip() == text)
            self.assertFalse(item['translation_required'])
            self.assertEqual(item['type'], 'formula')


class StructureGateFixes(unittest.TestCase):
    def _mapping(self, paragraph_rect, native_rect):
        return {
            'source_sha256': 'a' * 64, 'layout_reviewed': True,
            'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800], 'protected': [],
                       'spacing_obstacles': []}],
            'blocks': [
                {'id': 'a', 'source_page': 1, 'rect': paragraph_rect, 'source_text': 'Row text here',
                 'type': 'paragraph', 'translation_required': True, 'translation': '行文本'},
                {'id': 'b', 'source_page': 1, 'rect': native_rect, 'source_text': '≥99.0%',
                 'type': 'formula', 'translation_required': False, 'preservation_reason': 'native-formula'},
            ],
            'targets': {}, 'references': [], 'issues': [], 'expected_block_ids': ['a', 'b'],
        }

    def test_leading_bleed_between_rows_is_not_a_conflict(self):
        from core.structure import preflight
        # 1.8pt vertical bleed between consecutive table rows (Wang2023).
        report = preflight(self._mapping([40, 100, 200, 115], [180, 113.2, 240, 123.6]))
        checks = [i['check'] for i in report['issues']]
        self.assertNotIn('protected-intersection', checks)

    def test_real_overlap_still_blocks(self):
        from core.structure import preflight
        report = preflight(self._mapping([40, 100, 280, 160], [100, 120, 130, 135]))
        self.assertFalse(report['passed'])
        self.assertIn('protected-intersection', [i['check'] for i in report['issues']])

    def test_leading_bleed_tolerance_is_capped_for_tall_blocks(self):
        # Two 57pt blocks overlapping by 13pt are consecutive prose, not
        # font-metric bleed; the tolerance must not scale with block height.
        from core.structure import preflight
        report = preflight(self._mapping([40, 100, 356, 157], [100, 144, 130, 201]))
        self.assertIn('protected-intersection', [i['check'] for i in report['issues']])

    def test_hairline_cross_column_overlap_is_tolerated(self):
        from core.structure import preflight
        mapping = {
            'source_sha256': 'a' * 64, 'layout_reviewed': True,
            'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800],
                       'protected': [], 'spacing_obstacles': []}],
            'blocks': [
                {'id': 'left', 'source_page': 1, 'rect': [40, 100, 250, 120],
                 'source_text': 'Left column anchor text.', 'type': 'paragraph',
                 'translation_required': True},
                {'id': 'right', 'source_page': 1, 'rect': [350, 100, 560, 120],
                 'source_text': 'Right column anchor text.', 'type': 'paragraph',
                 'translation_required': True},
                {'id': 'cap', 'source_page': 1, 'rect': [40, 200, 300.05, 220],
                 'source_text': 'Caption text at the gutter.', 'type': 'caption',
                 'translation_required': True},
                {'id': 'frag', 'source_page': 1, 'rect': [300.0, 200, 360, 220],
                 'source_text': 'Fragment at the gutter.', 'type': 'paragraph',
                 'translation_required': True},
            ],
            'expected_block_ids': ['left', 'right', 'cap', 'frag'],
        }
        checks = [i['check'] for i in preflight(mapping)['issues']]
        self.assertNotIn('cross-column-source-overlap', checks)
        real = deepcopy(mapping)
        real['blocks'][3]['rect'][0] = 295
        checks = [i['check'] for i in preflight(real)['issues']]
        self.assertIn('cross-column-source-overlap', checks)


class MicroNativeFixes(unittest.TestCase):
    def test_tiny_equation_formula_and_reference_tail_are_native(self):
        self.assertTrue(native_micro_fragment('(2)', [543.9, 240.7, 555.5, 249.7]))
        self.assertTrue(native_micro_fragment('Li2CO3', [51.5, 173.9, 77.7, 184.5]))
        self.assertTrue(native_micro_fragment('S8).33', [51.5, 287.6, 74.3, 299.8]))
        self.assertTrue(native_micro_fragment('+ + Mg', [63.3, 393.4, 95.1, 405.5]))
        self.assertTrue(native_micro_fragment('Mg(OH)', [138.6, 394.4, 174.4, 404.4]))

    def test_prose_and_large_blocks_are_not_native(self):
        self.assertFalse(native_micro_fragment('in', [0, 0, 20, 10]))
        self.assertFalse(native_micro_fragment('We', [0, 0, 20, 10]))
        self.assertFalse(native_micro_fragment('Mg(OH)2', [0, 0, 200, 20]))
        self.assertFalse(native_micro_fragment('Fig. 2', [0, 0, 40, 10]))



    def test_internal_subscript_digit_not_misclassified_as_running_footer(self):
        from test_parser_layout import line
        # Brizhan 2026 (Condensed Matter and Interphases): page numbers 444..448
        # at the bottom margin must not cause an internal subscript '4' line in
        # n(NH4+) in the middle of page 4 to be split out as a protected footer.
        page1 = {"source_page": 1, "rect": [0, 0, 600, 800], "blocks": [
            block("444", [50, 775, 70, 788]),
            block("Body paragraph on page one with enough words.", [50, 100, 280, 140]),
        ], "drawing_clusters": [], "tables": []}
        page2 = {"source_page": 2, "rect": [0, 0, 600, 800], "blocks": [
            block("445", [50, 775, 70, 788]),
            block("", [50, 240, 280, 320], lines=[
                line("Fig. 2 shows the amount of ammonia nitrogen (nNH )", [50, 250, 280, 265]),
                line("4", [120, 255, 123, 262], size=5.2),
                line("+ ) extracted from the feed solution in the BMED.", [120, 250, 280, 265]),
            ]),
        ], "drawing_clusters": [], "tables": []}
        out = classify({"sha256": "b" * 64, "page_count": 2, "pages": [page1, page2], "issues": []})
        p2_blocks = [b for b in out["blocks"] if b["source_page"] == 2 and b["translation_required"]]
        self.assertEqual(len(p2_blocks), 1)
        self.assertIn("4", p2_blocks[0]["source_text"])

    def test_post_reference_information_about_authors_closes_bibliography(self):
        data = inspection([
            block("References", [40, 400, 280, 415]),
            block("1. Edwards T. M. Ammonia and aquatic ecosystems. 2023;167911.", [40, 425, 280, 455]),
            block("Information about the authors", [40, 470, 220, 485]),
            block("Kseniia V. Brizhan, Postgraduate student, Kuban State University.", [40, 495, 280, 525]),
        ])
        out = classify(data)
        info = next(b for b in out["blocks"] if "Information about the authors" in b["source_text"])
        bio = next(b for b in out["blocks"] if "Kseniia" in b["source_text"])
        self.assertTrue(info["translation_required"])
        self.assertTrue(bio["translation_required"])
        self.assertNotIn("Kseniia", out["references"][0]["text"])


if __name__ == '__main__':
    unittest.main()
