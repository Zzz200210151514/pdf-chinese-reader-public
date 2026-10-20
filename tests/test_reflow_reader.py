import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from reflow_reader import prepare, finish, safe_markup, media_at_readable_size
import mineru_extract


class ReflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.pdf'
        d = pymupdf.open()
        p = d.new_page(width=400, height=400)
        p.insert_text((40, 60), 'First native paragraph. NH4SCN 82.3%.')
        p.insert_text((40, 100), 'Second native paragraph.')
        d.save(self.source)
        d.close()

    def setup_input(self, blocks):
        path = self.root / 'content.json'
        path.write_text(json.dumps(blocks), encoding='utf-8')
        return path

    def test_empty_mineru_text_is_recovered_and_queued(self):
        data = self.setup_input([{'type': 'text', 'text': '', 'page_idx': 0,
                                 'bbox': [80, 110, 900, 165]}])
        m = prepare(self.source, data, self.root / 'work')
        self.assertIn('NH4SCN', m['blocks'][0]['source_text'])
        self.assertEqual(len(m['pending_jobs']), 1)
        self.assertEqual(m['blocks'][0]['review_flags'], ['mineru-text-empty'])
        with self.assertRaisesRegex(ValueError, 'Missing translations'):
            finish(self.root / 'work', {}, self.root / 'bad.pdf')
        self.assertFalse((self.root / 'bad.pdf').exists())

    def test_unknown_content_and_bad_page_are_not_silently_dropped(self):
        for item in [{'type': 'unrecognized', 'page_idx': 0, 'bbox': [0, 0, 1000, 1000]},
                     {'type': 'text', 'text': 'x', 'page_idx': 3, 'bbox': [0, 0, 1000, 1000]},
                     {'type': 'text', 'text': 'x', 'page_idx': 0, 'bbox': [0, 0, float('nan'), 1000]}]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                prepare(self.source, self.setup_input([item]), self.root / 'work')

    def test_markup_cannot_inject_external_images(self):
        self.assertEqual(safe_markup('NH<sub>4</sub>'), 'NH<sub>4</sub>')
        self.assertNotIn('<img', safe_markup('<img src="https://example.org/x">'))

    def test_long_translation_reflows_and_every_block_survives(self):
        data = self.setup_input([
            {'type': 'text', 'text': 'First', 'page_idx': 0, 'bbox': [80, 110, 900, 165]},
            {'type': 'text', 'text': 'Second', 'page_idx': 0, 'bbox': [80, 210, 900, 270]},
        ])
        work = self.root / 'work'
        m = prepare(self.source, data, work)
        zh = {m['blocks'][0]['id']: '首段开始。' + '中文长段落允许自动换页。' * 600 + '首段结束。',
              m['blocks'][1]['id']: '末段完成标记。'}
        result = finish(work, zh, self.root / 'reading.pdf')
        self.assertEqual(result['status'], 'passed')
        self.assertGreater(result['pages'], 1)
        pdf = pymupdf.open(self.root / 'reading.pdf')
        text = ''.join(p.get_text() for p in pdf).replace('\n', '').replace(' ', '')
        self.assertIn('首段开始。', text)
        self.assertIn('首段结束。', text)
        self.assertIn('末段完成标记。', text)
        self.assertEqual(set(result['rendered_block_ids']), {b['id'] for b in m['blocks']})

    def test_changed_source_blocks_finish(self):
        work = self.root / 'work'
        m = prepare(self.source, self.setup_input([
            {'type': 'text', 'text': 'First', 'page_idx': 0, 'bbox': [80, 110, 900, 165]}
        ]), work)
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Source PDF changed'):
            finish(work, {m['blocks'][0]['id']: '译文'}, self.root / 'bad.pdf')

    def test_media_moves_to_next_page_instead_of_becoming_tiny(self):
        work = self.root / 'work'
        m = prepare(self.source, self.setup_input([
            {'type': 'text', 'text': 'First', 'page_idx': 0, 'bbox': [80, 110, 900, 165]},
            {'type': 'image', 'image_caption': ['Figure caption'], 'page_idx': 0,
             'bbox': [100, 400, 900, 950]},
        ]), work)
        t = {m['blocks'][0]['id']: '正文内容。' * 420,
             m['blocks'][1]['id']: '图 1：原图应保持可读尺寸。'}
        report = finish(work, t, self.root / 'reading.pdf')
        self.assertTrue(report['media_page_breaks'])
        placement = report['media_placements'][0]
        self.assertGreaterEqual(placement['actual']['height'], placement['height'] * .99)

    def test_changed_original_media_blocks_delivery(self):
        work = self.root / 'work'
        m = prepare(self.source, self.setup_input([
            {'type': 'image', 'image_caption': ['caption'], 'page_idx': 0,
             'bbox': [100, 400, 900, 950]},
        ]), work)
        (work / m['blocks'][0]['asset']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Missing or changed original media'):
            finish(work, {m['blocks'][0]['id']: '图注'}, self.root / 'bad.pdf')

    def test_prepared_work_cannot_be_overwritten(self):
        data = self.setup_input([
            {'type': 'text', 'text': 'First', 'page_idx': 0, 'bbox': [80, 110, 900, 165]}
        ])
        prepare(self.source, data, self.root / 'work')
        with self.assertRaisesRegex(ValueError, 'Work already prepared'):
            prepare(self.source, data, self.root / 'work')

    def test_structured_cache_is_separate_from_markdown_cache(self):
        calls = []
        def run(command, **kwargs):
            if command[-1] == 'version':
                return SimpleNamespace(stdout='v-test', stderr='', returncode=0)
            calls.append(command)
            out = Path(command[command.index('-o') + 1])
            if '--format' in command:
                (out / 'document.md').write_text('text', encoding='utf-8')
                (out / 'document.json').write_text('[]', encoding='utf-8')
            else:
                out.write_text('text', encoding='utf-8')
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        argv = ['mineru_extract.py', '--source', str(self.source), '--output', str(self.root / 'cache'),
                '--cli', 'fake.exe']
        with patch.object(mineru_extract, 'stored_token', return_value=None), \
                patch.object(mineru_extract.subprocess, 'run', run), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(sys, 'argv', argv):
                mineru_extract.main()
            with patch.object(sys, 'argv', argv + ['--structured']):
                mineru_extract.main()
                mineru_extract.main()
        self.assertEqual(len(calls), 2)
        self.assertIn('--format', calls[-1])

    def test_structured_extraction_requires_json(self):
        def run(command, **kwargs):
            if command[-1] == 'version':
                return SimpleNamespace(stdout='v-test', stderr='', returncode=0)
            (Path(command[command.index('-o') + 1]) / 'document.md').write_text('text', encoding='utf-8')
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        argv = ['mineru_extract.py', '--source', str(self.source), '--output', str(self.root / 'cache'),
                '--cli', 'fake.exe', '--structured']
        with patch.object(mineru_extract, 'stored_token', return_value=None), \
                patch.object(mineru_extract.subprocess, 'run', run), \
                patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                mineru_extract.main()

    def test_double_columns_keep_title_and_wide_figure_across_columns(self):
        work = self.root / 'work'
        m = prepare(self.source, self.setup_input([
            {'type': 'text', 'text': 'Title', 'text_level': 1, 'page_idx': 0, 'bbox': [80, 110, 900, 165]},
            {'type': 'text', 'text': '1. Introduction', 'text_level': 2, 'page_idx': 0, 'bbox': [80, 210, 900, 270]},
            {'type': 'text', 'text': 'Body', 'page_idx': 0, 'bbox': [80, 110, 900, 165]},
            {'type': 'image', 'image_caption': ['Figure caption'], 'page_idx': 0,
             'bbox': [100, 400, 900, 950]},
        ]), work)
        t = {m['blocks'][0]['id']: '双栏论文样张标题', m['blocks'][1]['id']: '1. 引言',
             m['blocks'][2]['id']: '左栏开头。' + '双栏阅读需要保留正文内容。' * 105 + '右栏终点。',
             m['blocks'][3]['id']: '图 1：通栏图片保留完整内容。'}
        report = finish(work, t, self.root / 'reading.pdf', layout='double')
        self.assertEqual(report['layout']['columns'], 2)
        with pymupdf.open(self.root / 'reading.pdf') as pdf:
            left = [r for page in pdf for r in page.search_for('左栏开头')][0]
            right = [r for page in pdf for r in page.search_for('右栏终点')][0]
            self.assertLess(left.x0, 200)
            self.assertGreater(right.x0, 290)
        image = report['media_placements'][0]
        self.assertGreater(image['actual']['width'], 300)
        self.assertGreaterEqual(image['actual']['height'], image['height'] * .99)

    def test_browser_pixel_rounding_is_not_mistaken_for_formula_shrink(self):
        expected = {'width': 133.94, 'height': 35.66}
        self.assertTrue(media_at_readable_size({'width': 134.25, 'height': 35.25}, expected))
        self.assertFalse(media_at_readable_size({'width': 9.04, 'height': 3.27}, expected))

    def test_complete_paragraph_can_cover_reviewed_continuation_without_fragment_translation(self):
        work=self.root/'whole-paragraph'
        m=prepare(self.source,self.setup_input([
            {'type':'text','text':'First part','page_idx':0,'bbox':[80,110,900,165]},
            {'type':'text','text':'Continuation','page_idx':0,'bbox':[80,210,900,270]},
        ]),work)
        a,z=[b['id'] for b in m['blocks']]
        zh={a:'按完整段落组织中文，合并前后两部分的意思。'}
        report=finish(work,zh,self.root/'whole.pdf',{z:{'translation_in':a,'note':'已对照来源，为完整句子的两个来源块'}})
        self.assertEqual(set(report['rendered_block_ids']),{a,z})
        self.assertEqual(report['merged_translations'],{z:a})
        with pymupdf.open(self.root/'whole.pdf') as doc:
            self.assertIn('合并前后两部分的意思', ''.join(p.get_text() for p in doc))


if __name__ == '__main__':
    unittest.main()
