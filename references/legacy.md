# 旧版四步流程（V4 之前，仅兼容维护）

> 新任务一律走 V4.1 `prepare → review/resolve → plan → translate → finish`（见 [v4.md](v4.md) 与 [v41/下一篇论文使用说明](v41/下一篇论文使用说明.md)）。本文档保留旧入口的用法，仅供维护已有工作目录和旧来源清单时参考，不再推荐用于新论文。

## 适用范围

- 已有旧 `source_map.json` / `layout.json` 和旧 ID 的工作目录。
- MD/HTML 导出入口（`reader_export.py`）保持原样，与流程版本无关。

## 四步

1. **提取一次。** 检查已有原文哈希、来源映射和译文，复用有效结果。原 PDF 提供页码、坐标、图和公式；MinerU 仅在已选择时提供读序/内容辅助。按需读取 [workflow.md](workflow.md) 处理跨栏、扫描页和引用。未指定范围的"试试"做 2–4 页。
2. **脚本准备。** `prepare_reader.py prepare` 将 inspect 结果转为来源清单：保存英文、ID、页码、坐标、哈希、图片保护区及书目候选，预标记图内标签、旋转文字、页眉页脚、连续编号书目和独立公式行，并列出缩写定义候选。按完整段落分批翻译，模型只输出 `{id: 中文}`，`merge` 合并。预标记可撤销（设回 `translation_required=true` 并删除 `preservation_reason`）。
3. **按格式输出。**
   - MD/HTML：读 [web-markdown.md](web-markdown.md)，用 `reader_export.py`；HTML 内嵌图片，MD 用相对路径配套 assets。
   - PDF：按 [manifest.md](manifest.md) 生成排版清单，`pdf_reader.py build` 先收集全部区域重叠、保护区冲突及溢出统一报出，修正后再构建。只替换文字，保护原图和公式，图号/文献编号用明确映射建链接。字体规则见 [pdf-fonts.md](pdf-fonts.md)。
4. **检查并交付。** 处理 `.validation.json` 的 `english_residue`（疑似漏翻）和 `outside_region_changes`（区域外像素变化）；用 `.overview.pdf` 拼图看全页，首页、报警页、复杂公式与跨页衔接另放大；在可用阅读器试点图号、文献和返回。附译页需用户授权，见 [pdf-appendix-translation.md](pdf-appendix-translation.md)。

## 迁移提示

- V4.1 prepare 已完成本地 inspection 时不重复提取。
- 旧来源清单沿用原 ID，不强制迁移；新书目默认原位，`explicitappendrefs=true` 才走旧 writer 的追加书目页。
