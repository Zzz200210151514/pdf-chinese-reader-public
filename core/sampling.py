"""Random page sampling check and human-readable delivery report generator."""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
import pymupdf as fitz

_CJK_RE = re.compile(r'[\u3400-\u9fff]')
_EN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]{2,}\b")


def sample_and_report(source, output, work, report, check=None, mapping=None, *,
                      sample_pages=3, seed=None):
    """Randomly sample up to ``sample_pages`` pages, audit them, and write ``<output>.report.md``."""
    output_path = Path(output).resolve()
    source_path = Path(source).resolve() if source else None
    work_path = Path(work).resolve() if work else output_path.parent
    samples_dir = output_path.with_suffix('.samples')
    samples_dir.mkdir(parents=True, exist_ok=True)

    with fitz.open(output_path) as out_doc:
        total_pages = len(out_doc)
        k = min(max(1, int(sample_pages or 3)), total_pages) if total_pages else 0
        rng = random.Random(seed)
        sampled_nums = sorted(rng.sample(range(1, total_pages + 1), k)) if k else []

        sample_details = []
        for pno in sampled_nums:
            page = out_doc[pno - 1]
            png_path = samples_dir / f'sample-page-{pno:03d}.png'
            page.get_pixmap(dpi=150).save(png_path)
            text = page.get_text()
            cjk_count = len(_CJK_RE.findall(text))
            en_words = _EN_WORD_RE.findall(text)
            bad_glyphs = text.count('\ufffd') + text.count('\x00')
            links = page.get_links()
            sample_details.append({
                'page': pno,
                'image': str(png_path),
                'cjk_chars': cjk_count,
                'english_word_tokens': len(en_words),
                'bad_glyph_chars': bad_glyphs,
                'link_count': len(links),
                'ok': bad_glyphs == 0,
            })

    extraction = (mapping or {}).get('extraction') or {}
    extractor_used = extraction.get('extractor') or 'unknown'
    extraction_warnings = extraction.get('warnings') or []

    fallbacks = list((report or {}).get('fallback_records') or (mapping or {}).get('fallback_records') or [])
    unresolved = list((report or {}).get('unresolved_links') or (mapping or {}).get('unresolved_links') or [])
    residue = list((report or {}).get('english_residue') or [])
    warnings = list((report or {}).get('warnings') or [])

    # Also detect preserved untranslated tables or large fallback blocks from mapping.
    preserved_tables = []
    for blk in (mapping or {}).get('blocks', []):
        if blk.get('type') == 'table' and not blk.get('translation_required', True):
            preserved_tables.append({
                'page': blk.get('source_page'),
                'id': blk.get('id'),
                'reason': blk.get('preservation_reason') or '复杂表格保留原表',
                'source_text': (blk.get('source_text') or '')[:120],
            })

    layout_mode = (report or {}).get('layout_mode', 'inplace')
    delivery_status = (report or {}).get('delivery_status', 'passed')

    sampling_summary = {
        'output': str(output_path),
        'source': str(source_path) if source_path else None,
        'layout_mode': layout_mode,
        'delivery_status': delivery_status,
        'total_pages': total_pages,
        'sampled_pages': sampled_nums,
        'samples': sample_details,
        'extractor': extractor_used,
        'extraction_warnings': extraction_warnings,
        'fallback_count': len(fallbacks),
        'fallbacks': fallbacks,
        'preserved_tables_count': len(preserved_tables),
        'preserved_tables': preserved_tables,
        'unresolved_links_count': len(unresolved),
        'unresolved_links': unresolved,
        'english_residue_count': len(residue),
        'warning_count': len(warnings),
        'note': '抽查结果和机械检查仅反映抽样页及可机检项；抽查不能证明全文每处都准确。',
    }
    if work_path:
        work_path.mkdir(parents=True, exist_ok=True)
        (work_path / 'sampling-report.json').write_text(
            json.dumps(sampling_summary, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    lines = [
        '# 中文 PDF 交付说明与抽查报告',
        '',
        f'- **交付文件**：`{output_path}`',
        f'- **交付状态**：`{delivery_status}`（排版模式：`{layout_mode}`，总页数：{total_pages} 页）',
        f'- **提取方式**：`{extractor_used}`'
        + (f'（备注：{"; ".join(w.get("detail", w.get("check", "")) for w in extraction_warnings)}）'
           if extraction_warnings else ''),
        '',
        f'## 随机抽查结果（共抽查 {len(sampled_nums)} 页：{sampled_nums}）',
        '',
        '> 声明：以下仅报告随机抽查页与机械检查指标；抽查不能证明全文每处都准确。',
        '',
        '| 页码 | 中文字数 | 英文词数 | 乱码/缺字数 | 页内链接数 | 抽查截图 |',
        '|---:|---:|---:|---:|---:|---|',
    ]
    for s in sample_details:
        lines.append(
            f"| 第 {s['page']} 页 | {s['cjk_chars']} | {s['english_word_tokens']} | "
            f"{s['bad_glyph_chars']} | {s['link_count']} | `{s['image']}` |"
        )

    lines.extend(['', '## 保留原文 / 未翻译位置清单', ''])
    if fallbacks or preserved_tables or residue:
        for item in fallbacks:
            snippet = (item.get('source_text') or '').replace('\n', ' ')[:80]
            lines.append(
                f"- **第 {item.get('page', '?')} 页**（`{item.get('id', '')}` / {item.get('type', 'block')}）："
                f"{item.get('reason', '保留原文')}" + (f" — 原文：`{snippet}`" if snippet else '')
            )
        for tbl in preserved_tables:
            snippet = (tbl.get('source_text') or '').replace('\n', ' ')[:80]
            lines.append(
                f"- **第 {tbl.get('page', '?')} 页**（原表 `{tbl.get('id', '')}`）："
                f"{tbl.get('reason', '保留原表')}" + (f" — 摘录：`{snippet}`" if snippet else '')
            )
        for res in residue:
            snippet = (res.get('text') or '').replace('\n', ' ')[:80]
            lines.append(
                f"- **第 {res.get('page', '?')} 页**（未译英文残留，约 {res.get('english_words', 0)} 词）：`{snippet}`"
            )
    else:
        lines.append('- 无（全部可译正文均已覆盖）。')

    lines.extend(['', '## 无法准确跳转或尚未核实的引用清单', ''])
    if unresolved:
        for ul in unresolved:
            pg_str = f"第 {ul['page']} 页 · " if ul.get('page') else ''
            lines.append(f"- {pg_str}`{ul.get('key', '')}`（{ul.get('reason', '无法确认目标位置')}）")
    else:
        lines.append('- 机械检查未发现缺失目标的引用链接（实际点击体验仍以阅读器为准）。')

    report_md_path = output_path.with_suffix('.report.md')
    report_md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    sampling_summary['report_md'] = str(report_md_path)
    return sampling_summary
