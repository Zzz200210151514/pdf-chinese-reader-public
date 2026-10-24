"""Layout-consistency fixes: logical paragraphs, headings, audits, and soft gates."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'scripts'))

import pymupdf as fitz

from core.classifier import adapted_body_size, classify, native_micro_fragment
from core.layout import compile_layout
from core.paragraphs import link_paragraphs, prepare_render_text, split_flow
from core.pdf_reader import script_markup
from core.pipeline import resolve_issue, write
from core.redaction import check_erasure_coverage
from core.scientific_markup import restore_scientific_markup
from core.structure import preflight
from core.validator import _text_covers, validate
from knowledge import Glossary
from llm import Router
from llm.freeze import audit, freeze, restore
from pdf_reader import check_layout
from test_parser_layout import block, inspection
from test_router_terms import source_map
from test_structure_v41 import mapping


def _prose(ident, text, rect, page=1, size=8.0):
    return {'id': ident, 'source_page': page, 'rect': list(rect), 'source_text': text,
            'type': 'paragraph', 'translation_required': True, 'translation': '',
            'source_font_pt': size, 'size': size, 'leading': 1.17, 'min_scale': 0.85}


class HeadingAndFontTests(unittest.TestCase):
    def test_all_caps_headings_are_not_native_micro_fragments(self):
        box = [40, 100, 130, 112]
        for text in ('1. INTRODUCTION', '2. EXPERIMENTAL', '3. RESULTS', 'HIGHLIGHTS', 'KEYWORDS'):
            self.assertFalse(native_micro_fragment(text, box), text)
        self.assertTrue(native_micro_fragment('Li2CO3', [51.5, 173.9, 77.7, 184.5]))
        self.assertTrue(native_micro_fragment('(2)', [40, 100, 55, 110]))

    def test_classify_keeps_all_caps_heading_translatable(self):
        data = inspection([
            block('HIGHLIGHTS', [40, 100, 120, 112]),
            block('1. INTRODUCTION', [40, 130, 150, 142]),
            block('Li2CO3', [51.5, 173.9, 77.7, 184.5]),
        ])
        out = classify(data)
        by_text = {re.sub(r'<[^>]+>', '', b['source_text']): b for b in out['blocks']}
        self.assertTrue(by_text['HIGHLIGHTS']['translation_required'])
        self.assertNotEqual(by_text['HIGHLIGHTS']['type'], 'formula')
        self.assertTrue(by_text['1. INTRODUCTION']['translation_required'])
        self.assertFalse(by_text['Li2CO3']['translation_required'])

    def test_body_size_tracks_source_inside_reading_range(self):
        self.assertEqual(adapted_body_size(8), 8.5)
        self.assertEqual(adapted_body_size(8.5), 8.9)
        self.assertEqual(adapted_body_size(12), 9.5)
        data = inspection([block('The membrane flux stayed stable across the whole experiment.', [40, 100, 280, 130], size=8)])
        out = classify(data)
        body = next(b for b in out['blocks'] if b.get('translation_required'))
        self.assertEqual(body['source_font_pt'], 8)
        self.assertEqual(body['size'], 8.5)
        self.assertEqual(body['min_scale'], 0.85)


class FreezeGlossaryTests(unittest.TestCase):
    def test_roman_words_and_acronyms_are_not_frozen_as_chemistry(self):
        source = 'Complex II and III OF NF membranes hold H2O.'
        frozen, tokens = freeze(source)
        values = set(tokens.values())
        for word in ('II', 'III', 'OF', 'NF'):
            self.assertNotIn(word, values)
            self.assertIn(word, frozen)
        self.assertTrue(any('H2O' in value for value in values))

    def test_duplicate_placeholder_is_kept_and_unknown_is_rejected(self):
        text, tokens = freeze('The loading was 10 mg.')
        token = next(iter(tokens))
        self.assertEqual(restore(text.replace(token, token + token, 1), tokens).count('10'), 2)
        with self.assertRaises(ValueError):
            restore(text + '⟦V4_deadbeef_0099⟧', tokens)

    def test_numeric_count_mismatch_is_a_warning(self):
        tripled = audit('The flux tripled during the run.', '通量增加了3倍。')
        self.assertEqual(tripled.get('severity'), 'warning')
        self.assertIn('3', tripled['added'])
        figure = audit('Fig. 1a and b shows the trend.', '图 1a 和图 1b 显示该趋势。')
        self.assertEqual(figure.get('severity'), 'warning')
        router = Router({})
        rendered = router.translate(source_map('The flux tripled during the run.'),
                                    lambda job: {'P01B001': '通量增加了3倍。'})
        self.assertIn('3倍', rendered['P01B001'])
        self.assertTrue(any(r.get('check') == 'numeric-unit-audit' for r in router.records))

    def test_chemistry_loss_is_still_an_error(self):
        with self.assertRaises(ValueError):
            Router({}).translate(source_map('The H₂O concentration is 10 mg L−1 [2].'),
                                 lambda job: {'P01B001': '浓度下降。'})

    def test_forbidden_term_is_warning_unless_target_or_counterpart_present(self):
        glossary = Glossary()
        hit = glossary.validate('membrane rejection', '膜的排斥', 'membrane')
        self.assertTrue(hit and all(item['severity'] == 'warning' for item in hit))
        self.assertFalse(glossary.validate('The flux and the flow rate rose', '通量与流量上升', 'membrane'))
        self.assertFalse(glossary.validate('electrostatic repulsion and rejection', '存在排斥', 'membrane'))


class ScriptAndCitationTests(unittest.TestCase):
    def test_source_spans_are_pre_tagged_without_double_wrapping(self):
        sup = [{'spans': [{'text': 'R', 'size': 10, 'origin': [0, 20], 'flags': 0},
                          {'text': '2', 'size': 6, 'origin': [8, 16], 'flags': 1}]}]
        sub = [{'spans': [{'text': 'E', 'size': 10, 'origin': [0, 20], 'flags': 0},
                          {'text': 'cell', 'size': 6, 'origin': [8, 24], 'flags': 0}]}]
        self.assertEqual(script_markup(sup), 'R<sup>2</sup>')
        self.assertEqual(script_markup(sub), 'E<sub>cell</sub>')
        self.assertEqual(script_markup([{'spans': [{'text': 'R<sup>2</sup>', 'size': 10, 'origin': [0, 20]}]}]),
                         'R<sup>2</sup>')
        charge = [{'spans': [{'text': 'Ca', 'size': 10, 'origin': [0, 20]},
                             {'text': '2+', 'size': 6, 'origin': [12, 16]}]}]
        self.assertEqual(restore_scientific_markup('Ca<sup>2+</sup>', charge), 'Ca<sup>2+</sup>')
        frozen, _tokens = freeze('R<sup>2</sup> and E<sub>cell</sub>')
        self.assertEqual(frozen.count('<sup>'), 1)
        self.assertEqual(frozen.count('</sup>'), 1)
        self.assertNotIn('<sup><sup>', frozen)

    def test_citation_alignment_is_a_manifest_warning(self):
        lines = [{'spans': [{'text': 'Earlier studies.', 'size': 10, 'origin': [40, 100], 'color': 0},
                            {'text': '12', 'size': 7, 'origin': [120, 96], 'color': 0x2156aa, 'flags': 1}]}]
        mapping = {'layout_reviewed': True, 'source_sha256': 'a' * 64,
                   'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800], 'protected': []}],
                   'targets': {'ref/12': {'source_page': 1, 'point': [40, 700]}},
                   'references': [],
                   'blocks': [{'id': 'p', 'source_page': 1, 'rect': [40, 80, 320, 130],
                               'source_text': 'Earlier studies.', 'translation': '参见 12 以及研究 12。',
                               'translation_required': True, 'type': 'paragraph', 'source_lines': lines}]}
        mapping['expected_block_ids'] = ['p']
        manifest = compile_layout(mapping)
        self.assertTrue(any(item.get('check') == 'citation-alignment-needs-review' and item.get('severity') == 'warning'
                            for item in manifest['warnings']))


class LogicalParagraphTests(unittest.TestCase):
    def test_geometric_continuation_links_an_open_sentence(self):
        blocks = [
            _prose('a', 'The measured water flux was', [40, 100, 280, 130]),
            _prose('b', 'higher than the control value.', [40, 132, 280, 160]),
        ]
        mapping = {'blocks': blocks, 'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800]}],
                   'expected_block_ids': ['a', 'b']}
        link_paragraphs(mapping)
        self.assertEqual(blocks[1]['translation_in'], 'a')
        self.assertTrue(blocks[1]['join_previous'])
        self.assertEqual(blocks[0]['logical_paragraph_id'], blocks[1]['logical_paragraph_id'])
        self.assertIn('higher than the control', blocks[0]['logical_source'])
        self.assertEqual(blocks[0]['size'], blocks[1]['size'])
        self.assertEqual(blocks[0]['leading'], blocks[1]['leading'])

    def test_finished_sentence_and_heading_stay_separate(self):
        blocks = [
            _prose('a', 'The measured water flux was stable.', [40, 100, 280, 130]),
            _prose('b', 'another sentence follows here.', [40, 132, 280, 160]),
            _prose('h', 'HIGHLIGHTS', [40, 180, 140, 192]),
            _prose('c', 'points are listed here.', [40, 194, 280, 220]),
        ]
        blocks[2]['bold'] = True
        mapping = {'blocks': blocks, 'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800]}],
                   'expected_block_ids': [b['id'] for b in blocks]}
        link_paragraphs(mapping)
        self.assertNotIn('translation_in', blocks[1])
        self.assertNotIn('logical_paragraph_id', blocks[2])

    def test_mineru_paragraph_groups_split_boxes(self):
        blocks = [
            _prose('a', 'The rejection of salt', [40, 100, 280, 130]),
            _prose('b', 'remained above the limit.', [300, 100, 540, 140]),
        ]
        mapping = {'blocks': blocks, 'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800]}],
                   'expected_block_ids': ['a', 'b']}
        with tempfile.TemporaryDirectory() as temp:
            payload = [{'type': 'text', 'text': 'The rejection of salt remained above the limit.',
                        'bbox': [60, 110, 920, 190], 'page_idx': 0}]
            Path(temp, 'content_list.json').write_text(json.dumps(payload), encoding='utf-8')
            link_paragraphs(mapping, temp)
        self.assertEqual(blocks[1].get('translation_in'), 'a')
        self.assertIn('remained above', blocks[0]['logical_source'])

    def test_flow_splits_on_tag_boundaries_and_preserves_text(self):
        html = '前段<sup>12</sup>中段<i>斜体</i>以及<a href="https://pdf-reader.invalid/ref/1">链接</a>结尾'
        parts = split_flow(html, [1, 1, 1])
        self.assertIsNotNone(parts)
        self.assertEqual(''.join(parts), html)
        for part in parts:
            self.assertEqual(part.count('<sup>'), part.count('</sup>'))
            self.assertEqual(part.count('<i>'), part.count('</i>'))
            self.assertEqual(part.count('<a '), part.count('</a>'))
        self.assertGreater(sum(1 for part in parts if part), 1)

    def test_router_validates_the_merged_paragraph(self):
        mapping = {'blocks': [
            {'id': 'a', 'source_page': 1, 'type': 'paragraph', 'translation_required': True,
             'source_text': 'The flux was 10 mg', 'logical_source': 'The flux was 10 mg and then 20 mg',
             'logical_paragraph_id': 'lp-a'},
            {'id': 'b', 'source_page': 1, 'type': 'paragraph', 'translation_required': True,
             'source_text': 'and then 20 mg', 'translation_in': 'a', 'join_previous': True,
             'logical_paragraph_id': 'lp-a'},
        ]}
        jobs = Router({}).plan(mapping)
        ids = [b['id'] for job in jobs for b in job['blocks']]
        self.assertEqual(ids, ['a'])
        frozen = jobs[0]['blocks'][0]['source_text']
        tokens = re.findall(r'⟦V4_[a-f0-9]{8}_\d{4}⟧', frozen)
        self.assertGreaterEqual(len(tokens), 2)
        swapped = frozen.replace(tokens[0], '§A§').replace(tokens[1], tokens[0]).replace('§A§', tokens[1])
        rendered = Router({}).translate(mapping, lambda job: {'a': '先写' + swapped})
        self.assertIn('10', rendered['a'])
        self.assertIn('20', rendered['a'])

    def test_render_flow_uses_one_size_across_boxes(self):
        blocks = [
            _prose('a', 'The measured water flux was', [40, 700, 280, 760]),
            _prose('b', 'higher across the page break.', [40, 40, 280, 100], page=2),
        ]
        blocks[0]['translation'] = '水通量在栏末' * 4
        blocks[0]['logical_source'] = blocks[0]['source_text'] + ' ' + blocks[1]['source_text']
        mapping = {'layout_reviewed': True, 'source_sha256': 'b' * 64, 'references': [], 'targets': {},
                   'pages': [{'source_page': 1, 'rect': [0, 0, 600, 800], 'protected': []},
                             {'source_page': 2, 'rect': [0, 0, 600, 800], 'protected': []}],
                   'blocks': blocks}
        link_paragraphs(mapping)
        blocks[0]['translation'] = '水通量在页末继续并且在下一页写完余下的句子内容保持完整。'
        manifest = compile_layout(mapping)
        regions = [r for page in manifest['pages'] for r in page['regions']]
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]['size'], regions[1]['size'])
        self.assertEqual(regions[0]['leading'], regions[1]['leading'])
        self.assertEqual(regions[0].get('logical_paragraph_id'), regions[1].get('logical_paragraph_id'))
        combined = ''.join(re.sub(r'<[^>]+>', '', r['html']) for r in regions)
        self.assertIn('水通量', combined)
        self.assertEqual(regions[0]['size'], regions[1]['size'])


class GateAndValidatorTests(unittest.TestCase):
    def test_benign_classifier_candidates_do_not_block_plan(self):
        m = mapping()
        m['issues'] = [{'id': 'r', 'page': 1, 'check': 'caption-candidate-without-geometry'},
                       {'id': 'body', 'page': 1, 'check': 'inline-fragment-placement'},
                       {'id': 'heading', 'page': 1, 'check': 'bibliography-continuation-candidate'}]
        gate = preflight(m)
        self.assertTrue(gate['ready_to_dispatch'])
        self.assertTrue(all(i['severity'] == 'warning' for i in gate['issues']))
        with tempfile.TemporaryDirectory() as temp:
            write(Path(temp) / 'source_map.json', m)
            from core.pipeline import plan
            self.assertTrue(plan(temp, {'provider': 'host'}, True))

    def test_resolve_one_of_many_does_not_raise(self):
        m = mapping()
        m['issues'] = [{'id': 'r', 'page': 1, 'check': 'formula-with-prose-candidate'},
                       {'id': 'body', 'page': 1, 'check': 'formula-with-prose-candidate'}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / 'source_map.json', m)
            outcome = resolve_issue(root, 'r|formula-with-prose-candidate|1', 'prose wrap', {'provider': 'host'})
        self.assertFalse(outcome['map_frozen'])
        self.assertEqual(outcome['jobs'], 0)
        self.assertEqual(outcome['remaining_review'], 1)
        self.assertFalse(outcome['ready_to_dispatch'])

    def test_punctuation_or_two_character_erasure_residue_is_a_warning(self):
        from collections import Counter
        with fitz.open() as doc:
            page = doc.new_page()
            spec = {'source_page': 1, 'redactions_original': [[10, 10, 80, 30]],
                    'redactions': [[10, 10, 70, 30]], 'erase_guards': []}
            baseline = Counter()
            comma = Counter({('Helvetica', ord(','), 1.0, 2.0): 1})
            word = Counter({('Helvetica', ord(ch), float(i), 2.0): 1 for i, ch in enumerate('Word')})
            with patch('core.redaction._remaining', side_effect=[baseline, comma]):
                warned = check_erasure_coverage(page, spec)
            self.assertEqual(warned[0]['severity'], 'warning')
            with patch('core.redaction._remaining', side_effect=[baseline, word]):
                blocked = check_erasure_coverage(page, spec)
            self.assertEqual(blocked[0]['severity'], 'blocking')

    def test_neighbor_bleed_does_not_fail_searchable_text(self):
        self.assertTrue(_text_covers('Alpha', 'AlphaBeta'))
        self.assertFalse(_text_covers('AlphaBeta', 'Alpha'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, output = root / 's.pdf', root / 'o.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=300, height=400)
                page.insert_text((20, 50), 'Alpha', fontsize=10)
                page.insert_text((20, 58), 'Beta', fontsize=10)
                doc.save(source)
                doc.save(output)
            with fitz.open(output) as doc:
                alpha = doc[0].search_for('Alpha')[0]
                # Expand into the next line by more than a hair, inside leading bleed.
                rect = [alpha.x0, alpha.y0, alpha.x1 + 4, alpha.y1 + 6]
            manifest = {'pages': [{'source_page': 1, 'regions': [
                {'id': 'x', 'rect': rect, 'html': 'Alpha'}]}]}
            result = validate(source, output, manifest, {'blocks': []})
            self.assertTrue(result['text_regions'][0]['searchable_and_copyable'], result['text_regions'])

    def test_body_paragraph_scales_uniformly_instead_of_overflowing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)
            source = root / 'source.pdf'
            with fitz.open() as doc:
                doc.new_page(width=400, height=500)
                doc.save(source)
            manifest = {'fonts': {'regular': 'cjk.ttf', 'bold': 'cjk.ttf'},
                        'pages': [{'source_page': 1, 'regions': [
                            {'id': 'a', 'rect': [30, 40, 150, 78], 'block_type': 'paragraph',
                             'html': '水通量在窄栏中继续书写并且需要略微收紧。',
                             'size': 9.5, 'leading': 1.24, 'min_scale': 1.0, 'source_font_pt': 8,
                             'logical_paragraph_id': 'lp-body', 'source_block_ids': ['a']},
                            {'id': 'b', 'rect': [30, 90, 180, 140], 'block_type': 'paragraph',
                             'html': '后半段使用同一字号和行距。',
                             'size': 8.5, 'leading': 1.1, 'min_scale': 1.0, 'source_font_pt': 8,
                             'logical_paragraph_id': 'lp-body', 'source_block_ids': ['b']},
                        ]}]}
            report = check_layout(source, manifest, root)
            self.assertTrue(report['layout_checks_passed'], report['issues'])
            regions = manifest['pages'][0]['regions']
            self.assertEqual(regions[0]['size'], regions[1]['size'])
            self.assertEqual(regions[0]['leading'], regions[1]['leading'])
            self.assertTrue(report['warnings'])


if __name__ == '__main__':
    unittest.main()
