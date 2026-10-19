"""Native glyph bounds must constrain erasure, not Chinese placement."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf as fitz
from core.layout import compile_layout


def mapping():
    return {'layout_reviewed': True, 'source_sha256': 'a'*64,
            'pages': [{'source_page': 1, 'rect': [0, 0, 300, 400]}],
            'blocks': [
                {'id': 'marker', 'source_page': 1, 'rect': [40, 80, 47, 87],
                 'source_text': '*', 'type': 'figure', 'translation_required': False,
                 'preservation_reason': 'native-decorative-marker',
                 'source_lines': [{'spans': [{'text': '*', 'font': 'Helvetica',
                    'origin': [40, 90], 'bbox': [40, 65, 50, 96]}]}]},
                {'id': 'body', 'source_page': 1, 'rect': [40, 93, 200, 120],
                 'type': 'paragraph', 'source_text': 'Body', 'translation': '正文',
                 'translation_required': True}]}


class RedactionGuards(unittest.TestCase):
    def test_zero_advance_accent_does_not_block_other_guards(self):
        from core.redaction import split_masks
        self.assertEqual(split_masks([[10, 10, 30, 30]], [{'rect': [20, 0, 20, 40]}]),
                         [[10, 10, 30, 30]])

    def test_compile_splits_erase_boxes_without_moving_translation(self):
        page = compile_layout(mapping())['pages'][0]
        guard = fitz.Rect(40, 65, 50, 96)
        self.assertFalse(any(fitz.Rect(r).intersects(guard) for r in page['redactions']))
        self.assertEqual(page['regions'][0]['rect'][:3], [40, 93, 200])
        self.assertEqual(page['protected'][0]['rect'], [40, 80, 47, 87])
        self.assertEqual(page['redactions_original'], [[40, 93, 200, 120]])

    def test_guard_covering_target_does_not_silently_leave_english(self):
        from core.redaction import check_erasure_coverage
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 100), 'English')
            box = list(page.search_for('English')[0])
            spec = {'source_page': 1, 'redactions_original': [box], 'redactions': [],
                    'erase_guards': [{'rect': box, 'font': 'Helvetica',
                                      'origin': [40, 50], 'text': '*'}]}
            self.assertTrue(check_erasure_coverage(page, spec))

    def test_native_star_survives_but_adjacent_english_is_removed(self):
        from core.redaction import split_masks, check_erasure_coverage
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 90), '*', fontsize=32)
            page.insert_text((40, 104), 'English', fontsize=10)
            star = next(s for b in page.get_text('dict')['blocks'] for l in b['lines']
                        for s in l['spans'] if s['text'] == '*')
            guard = {'rect': list(star['bbox']), 'font': star['font'],
                     'origin': list(star['origin']), 'text': '*'}
            original = [list(page.search_for('English')[0])]
            spec = {'source_page': 1, 'redactions_original': original,
                    'redactions': split_masks(original, [guard]), 'erase_guards': [guard]}
            self.assertEqual(check_erasure_coverage(page, spec), [])
            for box in spec['redactions']:
                page.add_redact_annot(box, fill=False)
            page.apply_redactions(images=0, graphics=0, text=0)
            self.assertEqual(page.get_text().strip(), '*')

    def test_real_guard_cannot_hide_unerasable_prose(self):
        from core.redaction import check_erasure_coverage
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 100), '* English')
            span = page.get_text('dict')['blocks'][0]['lines'][0]['spans'][0]
            # A legitimate native span at another origin does not authorize
            # keeping English just because its rectangle overlaps a guard.
            page.insert_text((40, 50), '*')
            spec = {'source_page': 1, 'redactions_original': [list(span['bbox'])],
                    'redactions': [], 'erase_guards': [{'rect': list(span['bbox']),
                    'font': 'Helvetica', 'origin': [40, 50], 'text': '*'}]}
            self.assertEqual(check_erasure_coverage(page, spec)[0]['check'], 'erase-coverage-conflict')


if __name__ == '__main__':
    unittest.main()
