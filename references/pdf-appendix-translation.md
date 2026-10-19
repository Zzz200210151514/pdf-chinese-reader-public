# PDF 原文页与中文附译页

本路径保留来源页，随后添加中文译页，与原位双栏 PDF 是不同版式。只在用户明确要求该版式，或已授权从原位版式改为附译页时使用；局部公式冲突、溢出或一次像素检查失败不能触发整篇自动切换。先按 workflow.md 修复区域与保护边界。

附译页提供返回对应原文页的链接和目录书签；图与公式裁取原 PDF，不重绘科研数据。

## 清单格式

```json
{
  "title": "中文论文阅读版",
  "layout_mode": "appendix",
  "requested_layout": "appendix",
  "source_sha256": "可选：源 PDF 的 SHA-256",
  "pages": [
    {
      "source_page": 1,
      "blocks": [
        {"type": "heading", "text": "实验方法"},
        {"type": "paragraph", "text": "完整中文译文……"},
        {
          "type": "figure",
          "crop": {"source_page": 1, "rect": [80, 180, 520, 460]},
          "caption": "图1：完整中文图注。",
          "max_height": 190
        },
        {
          "type": "equation",
          "crop": {"source_page": 1, "rect": [100, 500, 480, 550]},
          "max_height": 110
        }
      ]
    }
  ]
}
```

- 上例表示用户直接要求附译页。若用户原本要求原位版式，保留 `requested_layout: "source"`，并且仅在已有明确授权时填写 `layout_change_authorized: true`。脚本在生成 PDF 前核对此约定，并拒绝字体缺字。
- `pages[].source_page` 和 `crop.source_page` 均为从 1 开始的源 PDF 页码；`rect` 使用 PDF 点坐标 `[x0,y0,x1,y1]`。
- 每个来源页的 `blocks` 按阅读顺序排列。正文、标题、公式说明和图注都必须完整翻译；公式/图表以 `figure`、`image` 或 `equation` 块插入源 PDF 裁图。
- 文本块使用 `type: paragraph` 或 `heading`；可选 `size`、`lineheight`。媒体块可选 `caption`、`caption_size`、`max_height`。过长段落应在清单里按语义拆分，脚本会在块与块之间分页。
- 源文页不要求全部纳入样张，但清单范围和输出标题不得声称全文。要做全文，须覆盖所有含正文的源页，并独立核对页数和覆盖。

## 构建与检查

```powershell
python -X utf8 scripts/append_translation_pdf.py `
  --source paper.pdf `
  --manifest work/append-translation.json `
  --output outputs/paper.zh.pdf `
  --font C:/Windows/Fonts/msyh.ttc
```

脚本生成 `.validation.json`，检查源页渲染像素与原 PDF 一致、译页包含可搜索中文、返回链接的目标有效，并记录构建耗时。默认通过功能检查即可交付；需要时快速看拼图，只放大实际异常，不把逐页巡视或手动点击作为默认门禁。自动检查不认证语义准确，实际查看或点击应分别如实记录。公式图片清晰度取决于源页和裁切框，检查裁图没有漏符号、式号或变量说明。

本路径不会自动提取译文、判断公式边界、检测译文完整性或补参考文献。模型负责读序、译文及裁切框；引用目标只在确有书目数据时建立，不对源 PDF 中缺失的参考文献表创建空链接。

