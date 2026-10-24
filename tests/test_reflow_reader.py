"""MinerU CLI cache checks, plus the deprecated reflow shim helpers.

HTML/Chromium reflow is no longer a second pipeline. In-place MinerU fusion is
covered by tests/test_extractor_hybrid.py.
"""
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
from reflow_reader import media_at_readable_size, safe_markup
import mineru_extract


class ReflowShimTests(unittest.TestCase):
    def test_markup_cannot_inject_external_images(self):
        self.assertEqual(safe_markup('NH<sub>4</sub>'), 'NH<sub>4</sub>')
        self.assertNotIn('<img', safe_markup('<img src="https://example.org/x">'))

    def test_browser_pixel_rounding_is_not_mistaken_for_formula_shrink(self):
        expected = {'width': 133.94, 'height': 35.66}
        self.assertTrue(media_at_readable_size({'width': 134.25, 'height': 35.25}, expected))
        self.assertFalse(media_at_readable_size({'width': 9.04, 'height': 3.27}, expected))


class MinerUExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.pdf'
        with pymupdf.open() as doc:
            page = doc.new_page(width=400, height=400)
            page.insert_text((40, 60), 'First native paragraph. NH4SCN 82.3%.')
            doc.save(self.source)

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


if __name__ == '__main__':
    unittest.main()
