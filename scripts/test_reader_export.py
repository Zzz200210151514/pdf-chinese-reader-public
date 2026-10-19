"""Run with Python; synthetic content only, no API or third-party dependency."""
import base64
import json
import re
import shutil
import tempfile
from pathlib import Path
from reader_export import build


def check():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'figure.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        data = {
            'title': '合成测试论文',
            'scope': '仅测试排版与导出功能',
            'meta': {
                'title_en': 'Synthetic Test Paper',
                'journal': 'Journal of Synthetic Tests',
                'article_type': 'Article',
                'doi': '10.1038/s41467-026-00000-0',
                'authors': [
                    {'name': 'Test Author', 'affiliations': [1], 'corresponding': True}
                ],
                'affiliations': ['Test Institute, Test City']
            },
            'blocks': [
                {'id': 'Abs1', 'type': 'abstract', 'translation': '这是摘要段落。包含 10 mA cm⁻² 和 Fe⁴⁺ 以及[[ref:1]]。'},
                {'id': 'S1', 'translation': '见[[fig:1|图 1]]、[[ref:1-2]]、[Fig:1a]、[[ref:2]]。面积（cm）²、平方项（x）²。化学式 ΔG<sub>OOH*</sub>。<script>alert(1)</script>'},
                *[{'id': k, 'type': t, 'number': 1, 'asset': 'figure.png', 'translation': '中文说明'} for k, t in [('F1', 'figure'), ('E1', 'equation'), ('T1', 'table')]]
            ],
            'references': [
                {'number': 1, 'text': 'First reference.'},
                {'number': 2, 'text': 'Second reference.'}
            ]
        }
        manifest = root / 'reader.json'

        def save():
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

        save()
        for fmt in ('md', 'html'):
            out = root / 'out' / ('reader.' + fmt)
            report = build(manifest, out, fmt)
            assert report['images'] == 3
            assert report['references'] == 2
            text = out.read_text(encoding='utf-8')
            assert '<script>alert(1)</script>' not in text
            if fmt == 'html':
                ids = re.findall(r'\bid="([^"]+)"', text)
                assert len(ids) == len(set(ids))
                assert all(t in ids for t in re.findall(r'href="#([^"]+)"', text))
                # verify superscripts, safe tags, and chemistry protection
                assert 'href="#ref-1"' in text
                assert 'href="#ref-2"' in text
                assert 'mA cm⁻²' in text  # preserved unit, not converted
                assert '面积（cm）²' in text and '平方项（x）²' in text
                assert 'Fe⁴⁺' in text     # preserved valence, not converted
                assert '<sub>OOH*</sub>' in text  # safe tag unescaped
                assert 'Test Author' in text
                assert 'Journal of Synthetic Tests' in text

        shutil.copytree(root / 'out', root / 'moved')
        md = (root / 'moved/reader.md').read_text(encoding='utf-8')
        assert all((root / 'moved' / a).is_file() for a in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', md))

        for mutation in ('missing', 'collision', 'traversal', 'coverage'):
            original = json.loads(manifest.read_text(encoding='utf-8'))
            if mutation == 'missing':
                data['blocks'][1]['translation'] = '[[ref:99]]'
            if mutation == 'collision':
                data['blocks'][1]['id'] = 'ref-1'
            if mutation == 'traversal':
                data['blocks'][2]['asset'] = '../outside.png'
            if mutation == 'coverage':
                data['expected_block_ids'] = ['missing']
            save()
            try:
                build(manifest, root / 'invalid.html', 'html')
                raise AssertionError('Invalid input accepted: ' + mutation)
            except ValueError:
                assert not (root / 'invalid.html').exists()
            data = original
            save()
    print('PASS: links, portable assets, metadata, abstract, safe tags, chemistry protection, citations, errors handled')


if __name__ == '__main__':
    check()
