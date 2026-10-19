"""Offline regression checks for extraction isolation and numeric audit."""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import mineru_extract
from translation_audit import numbers
from reader_export import Inline


def check():
    assert numbers('10 mA [[ref:1]]') == numbers('10 mA [[ref:1|文献 1]]')
    assert numbers('10 mA [[ref:1]]') != numbers('11 mA [[ref:1|文献 1]]')
    for fmt in ('md', 'html'):
        renderer = Inline(fmt, {'ref-2'})
        for value in ('面积（cm）²', '平方项（x）²', 'Fe⁴⁺', '¹⁸O'):
            assert renderer(value) == value
        assert len(renderer.links) == 0
        assert 'ref-2' in renderer('[[ref:2]]')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / 'paper.pdf'
        source.write_bytes(b'synthetic')
        mode = 'empty'
        calls = []
        def run(cmd, **kwargs):
            if cmd[-1] == 'version':
                return SimpleNamespace(stdout='v-test', stderr='', returncode=0)
            calls.append(cmd)
            if mode == 'valid':
                Path(cmd[cmd.index('-o') + 1]).write_text('Fresh extracted text', encoding='utf-8')
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        argv = ['mineru_extract.py', '--source', str(source), '--output', str(root / 'cache'), '--cli', 'fake.exe']
        with patch.object(sys, 'argv', argv), patch.object(mineru_extract.subprocess, 'run', run):
            for attempt in range(2):
                try:
                    mineru_extract.main()
                    raise AssertionError('Empty attempt accepted')
                except SystemExit as e:
                    assert e.code
                cache = next(p for p in (root / 'cache').iterdir() if '.previous-' not in p.name)
                assert not json.loads((cache / 'extraction-report.json').read_text())['success']
                (cache / 'document.md').write_text('Stale incomplete text', encoding='utf-8')
            mode = 'valid'
            mineru_extract.main()
            assert (cache / 'document.md').read_text() == 'Fresh extracted text'
            assert json.loads((cache / 'extraction-report.json').read_text())['success']
            n = len(calls)
            mineru_extract.main()
            assert len(calls) == n
    print('PASS: scientific superscripts, citation numbers, changed values, stale outputs, fresh extraction, cache hit')


if __name__ == '__main__':
    check()
