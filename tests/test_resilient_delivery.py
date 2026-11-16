"""End-to-end and unit tests for resilient delivery, MinerU wrapper resolution, reflow PDF, and 3-page sampling."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import pymupdf as fitz
import mineru_extract
import reader_export
from core.pipeline import finish, plan, prepare, write
from core.reflow_pdf import build_reflow_pdf_from_merged
from core.sampling import sample_and_report
from core.structure import preflight, reconcile_source_map
from core.doctor import doctor
from knowledge import Glossary
from llm import Router


class ResilientDeliveryTests(unittest.TestCase):
    def _make_sample_pdf(self, path: Path, pages: int = 4):
        with fitz.open() as doc:
            for p in range(pages):
                pg = doc.new_page(width=595, height=842)
                pg.insert_text((50, 60), f'Section {p + 1}. Bipolar Membrane Electrodialysis', fontsize=12)
                pg.insert_text((50, 100), 'This paragraph discusses water dissociation at the interface [1].', fontsize=10)
                pg.insert_text((50, 140), 'Second paragraph with NaCl 1.5 mol/L and current density 100 mA/cm2.', fontsize=10)
                if p == 0:
                    pg.draw_rect(fitz.Rect(50, 200, 260, 320), color=(0.1, 0.3, 0.7), fill=(0.9, 0.94, 0.99))
                    pg.insert_text((60, 260), 'Figure 1 schematic', fontsize=9)
                    pg.insert_text((50, 340), 'Fig. 1. Schematic of bipolar membrane interface.', fontsize=9)
                if p == pages - 1:
                    pg.insert_text((50, 500), 'References', fontsize=11)
                    pg.insert_text((50, 525), '[1] Melnikov et al., J. Membr. Sci., 2026.', fontsize=9)
            doc.save(path)

    def test_mineru_wrapper_resolves_npm_native_exe_or_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd_wrapper = root / 'mineru-open-api.cmd'
            cmd_wrapper.write_text('@echo off\n', encoding='utf-8')
            # Without native exe in node_modules, falls back cleanly to cmd.exe wrapper.
            self.assertEqual(
                mineru_extract._resolve_cli_prefix(str(cmd_wrapper)),
                ['cmd.exe', '/d', '/c', str(cmd_wrapper)],
            )
            # With npm native package binary present, resolves straight to native exe.
            native = (
                root / 'node_modules' / 'mineru-open-api' / 'node_modules'
                / 'mineru-open-api-win32-x64' / 'bin' / 'mineru-open-api.exe'
            )
            native.parent.mkdir(parents=True)
            native.write_bytes(b'MZ')
            ps1_wrapper = root / 'mineru-open-api.ps1'
            ps1_wrapper.write_text('# wrapper\n', encoding='utf-8')
            self.assertEqual(mineru_extract._resolve_cli_prefix(str(ps1_wrapper)), [str(native)])

    def test_router_merges_batch_types_and_tolerates_single_block_issues(self):
        mapping = {
            'blocks': [
                {'id': 'b1', 'source_page': 1, 'type': 'title', 'source_text': 'Membrane Study', 'translation_required': True},
                {'id': 'b2', 'source_page': 1, 'type': 'heading', 'source_text': '1. Introduction', 'translation_required': True},
                {'id': 'b3', 'source_page': 1, 'type': 'paragraph', 'source_text': 'The flux was 10 mg/L.', 'translation_required': True},
                {'id': 'b4', 'source_page': 1, 'type': 'caption', 'source_text': 'Fig. 1. Setup.', 'translation_required': True},
            ]
        }
        router = Router({'provider': 'host', 'delivery_mode': 'resilient', 'merge_batch_types': True, 'batch_size': 25})
        jobs = router.plan(mapping)
        self.assertEqual(len(jobs), 1)
        self.assertEqual([b['id'] for b in jobs[0]['blocks']], ['b1', 'b2', 'b3', 'b4'])

        # Corrupted placeholder in b3 falls back to source_text without failing b1/b2/b4.
        result = router.translate(
            mapping,
            supplied={
                'b1': '膜研究',
                'b2': '1. 引言',
                'b3': '通量为 ⟦V4_badtoken_9999⟧ 毫克/升。',
                'b4': '图 1. 装置。',
            },
        )
        self.assertEqual(result['b1'], '膜研究')
        self.assertEqual(result['b3'], 'The flux was 10 mg/L.')
        self.assertTrue(any(r.get('check') == 'placeholder-fallback-original' for r in router.records))

    def test_reconcile_source_map_clears_protected_intersection_without_shrinking_figure(self):
        fig_rect = [50, 200, 250, 320]
        mapping = {
            'source_sha256': 'a' * 64,
            'pages': [{'source_page': 1, 'rect': [0, 0, 595, 842], 'protected': [{'id': 'fig1', 'rect': list(fig_rect), 'type': 'figure'}]}],
            'blocks': [
                {'id': 'p1', 'source_page': 1, 'rect': [50, 100, 250, 130], 'source_text': 'Clean prose.', 'translation_required': True},
                {'id': 'overlap_lbl', 'source_page': 1, 'rect': [80, 230, 150, 250], 'source_text': 'Label inside figure', 'translation_required': True},
            ],
            'expected_block_ids': ['p1', 'overlap_lbl'],
            'issues': [{'id': 'p1', 'page': 1, 'check': 'formula-with-prose-candidate'}],
        }
        reconciled, fallbacks = reconcile_source_map(mapping)
        gate = preflight(reconciled, resilient=True)
        self.assertTrue(gate['ready_to_dispatch'])
        self.assertEqual(reconciled['pages'][0]['protected'][0]['rect'], fig_rect)
        self.assertFalse(reconciled['blocks'][1]['translation_required'])
        self.assertTrue(fallbacks)

    def test_resilient_inplace_and_reflow_pdf_with_3page_sampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            font_dir = root / 'fonts'
            font_dir.mkdir()
            (font_dir / 'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)

            source = root / 'paper.pdf'
            self._make_sample_pdf(source, pages=4)
            work = root / 'work'
            cfg = {
                'provider': 'host',
                'delivery_mode': 'resilient',
                'layout_mode': 'inplace',
                'merge_batch_types': True,
                'batch_size': 25,
                'sample_check_pages': 3,
                'fonts': {'regular': 'cjk.ttf', 'bold': 'cjk.ttf'},
            }
            mapping, _ = prepare(source, work, config=cfg, extractor='local')
            jobs = plan(work, config=cfg, reviewed=True)
            self.assertTrue(jobs)

            # Supply partial translations (deliberately omit one block and inject a huge overflow on one block).
            trans = {}
            translatable = [b for b in mapping['blocks'] if b.get('translation_required', True) and not b.get('translation_in')]
            for idx, b in enumerate(translatable):
                if idx == len(translatable) - 1:
                    continue  # omitted -> resilient fallback
                if idx == 1:
                    trans[b['id']] = '这是一个故意放大的超长溢出测试段落。' * 25
                else:
                    trans[b['id']] = f'中文译文段落 {idx + 1}，包含实验分析与结果讨论。'
            tpath = root / 'trans.json'
            tpath.write_text(json.dumps(trans, ensure_ascii=False), encoding='utf-8')

            out_inplace = root / 'out_inplace.pdf'
            report, _check = finish(
                source, work, out_inplace, font_dir,
                translations=tpath, config=cfg, reviewed=True,
                fonts={'regular': 'cjk.ttf', 'bold': 'cjk.ttf'},
                layout_mode='inplace',
            )
            self.assertTrue(out_inplace.is_file() and out_inplace.stat().st_size > 0)
            self.assertTrue(out_inplace.with_suffix('.report.md').is_file())
            self.assertEqual(len(list(out_inplace.with_suffix('.samples').glob('sample-page-*.png'))), 3)

            # Now test reflow PDF mode directly.
            out_reflow = root / 'out_reflow.pdf'
            reflow_report, _ = finish(
                source, work, out_reflow, font_dir,
                translations=tpath, config=cfg, reviewed=True,
                fonts={'regular': 'cjk.ttf', 'bold': 'cjk.ttf'},
                layout_mode='reflow',
            )
            self.assertTrue(out_reflow.is_file() and out_reflow.stat().st_size > 0)
            self.assertEqual(reflow_report['layout_mode'], 'reflow')
            self.assertTrue(out_reflow.with_suffix('.report.md').is_file())
            with fitz.open(out_reflow) as doc:
                full_text = ''.join(p.get_text() for p in doc)
                self.assertIn('中文译文段落', full_text)

    def test_reader_export_format_pdf_and_sampling_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            font_dir = root / 'fonts'
            font_dir.mkdir()
            (font_dir / 'cjk.ttf').write_bytes(fitz.Font('cjk').buffer)

            # Create a tiny figure asset.
            assets = root / 'assets'
            assets.mkdir()
            with fitz.open() as doc:
                pg = doc.new_page(width=200, height=100)
                pg.draw_rect(fitz.Rect(5, 5, 195, 95), color=(0, 0, 1), fill=(0.92, 0.96, 1))
                pg.get_pixmap(dpi=120).save(assets / 'fig1.png')

            manifest = {
                'title': '双极膜电渗析测试论文',
                'fonts': {'regular': 'cjk.ttf', 'bold': 'cjk.ttf'},
                'blocks': [
                    {'id': 'sec1', 'type': 'heading', 'level': 2, 'translation': '1. 引言'},
                    {'id': 'p1', 'type': 'paragraph', 'translation': '参见[[fig:1]]和文献[[ref:1]]。'},
                    {'id': 'f1', 'type': 'figure', 'number': 1, 'asset': 'assets/fig1.png', 'width_pt': 180, 'translation': '双极膜示意图。'},
                ],
                'references': [{'number': 1, 'text': 'Melnikov et al., 2026.'}],
            }
            mpath = root / 'manifest.json'
            mpath.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
            out_pdf = root / 'reader.pdf'
            rep = reader_export.build(mpath, out_pdf, 'pdf', font_dir=font_dir)
            self.assertTrue(out_pdf.is_file() and out_pdf.stat().st_size > 0)
            self.assertEqual(rep['layout_mode'], 'reflow')
            self.assertGreaterEqual(rep['links_created'], 2)

            summary = sample_and_report(None, out_pdf, root / 'work', rep, sample_pages=3, seed=42)
            self.assertEqual(summary['sampled_pages'], [1])
            self.assertTrue(Path(summary['report_md']).is_file())

    def test_csv_glossary_files_override_and_preserve_domain_rules(self):
        glossary = Glossary(glossary_files=['knowledge/terminology/interlinear-glossary-full.csv'])
        self.assertEqual(glossary.apply('co-ion leakage', '同离子泄漏', 'membrane'), '同离子泄漏')
        matched = {e['source'].lower(): e['target'] for e in glossary.inspect('bipolar membrane electrodialysis co-ion permeability', 'membrane')}
        self.assertEqual(matched.get('co-ion'), '同离子')
        self.assertEqual(matched.get('permeability'), '渗透系数')
        self.assertEqual(matched.get('bipolar membrane electrodialysis'), '双极膜电渗析')
        self.assertTrue(glossary.validate('water flux', '水流量', 'membrane'))

    def test_doctor_reports_skill_loaded_and_mineru_status_without_token_leak(self):
        rep = doctor()
        self.assertTrue(rep.get('skill_loaded'))
        self.assertIn('mineru', rep)
        self.assertIn('ready', rep['mineru'])
        self.assertIn('status_text', rep['mineru'])
        serialized = json.dumps(rep, ensure_ascii=False)
        self.assertNotIn('token_value', serialized)

    def test_first_time_friend_mineru_auth_config_is_detected_by_doctor(self):
        import os
        from unittest.mock import patch
        from core.mineru_hybrid import mineru_token

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            clean_env = {k: v for k, v in os.environ.items() if k not in ('MINERU_API_KEY', 'MINERU_TOKEN', 'MINERU_TOKEN_FILE')}
            with patch.dict(os.environ, clean_env, clear=True), \
                    patch('pathlib.Path.home', return_value=fake_home), \
                    patch('core.mineru_hybrid._windows_token', return_value=None):
                before = doctor()
                self.assertFalse(before['mineru']['token_configured'])
                self.assertIsNone(before['mineru']['token_source'])
                self.assertFalse(before['mineru']['ready'])

                # Simulate `mineru-open-api auth` writing ~/.mineru/config.yaml
                mineru_dir = fake_home / '.mineru'
                mineru_dir.mkdir(parents=True)
                (mineru_dir / 'config.yaml').write_text('token: sk-test-first-time-friend-0001\n', encoding='utf-8')

                self.assertEqual(mineru_token(), 'sk-test-first-time-friend-0001')
                after = doctor()
                self.assertTrue(after['mineru']['token_configured'])
                self.assertEqual(after['mineru']['token_source'], 'file:~/.mineru/config.yaml')
                self.assertEqual(after['mineru']['ready'], bool(after['mineru']['cli_found']))
                self.assertNotIn('sk-test-first-time-friend-0001', json.dumps(after, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
