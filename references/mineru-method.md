# MinerU 提取（统一流水线的前端）

MinerU 和本地 PyMuPDF 只在 `prepare` 分流。两者都写出同一份 `source_map.json`，然后走 `translator_v4.py` 的 `plan` → 翻译（Router、术语、缓存）→ `finish` 原位渲染。没有第二条 HTML/Chromium 重排流水线。`scripts/reflow_reader.py` 只保留弃用提示，并在仍被调用时转到这条流水线。

## 默认 `--extractor auto`

```powershell
python -X utf8 scripts/translator_v4.py prepare --source paper.pdf --work work/paper
```

`auto` 接受显式的 `auto`、`mineru`、`local`：

- 已配置 `MINERU_API_KEY`、`MINERU_TOKEN`，或凭据文件（`MINERU_TOKEN_FILE`、`~/.config/pdf-chinese-reader/mineru-token`，Windows 凭据管理器仍可用）时，运行 MinerU。
- 没有凭据，或 MinerU 调用/提取失败时，在 `source_map.json` 的 `extraction.warnings` 记一条非阻断警告，并自动改用 `local`。`prepare` 不因此失败。
- 显式 `--extractor mineru` 失败时仍报错，不悄悄改用本地结果。显式 `--extractor local` 不调用 MinerU。

凭据、上传授权和缓存见 [mineru.md](mineru.md)。

## 有文字层：MinerU 结构 + PyMuPDF 字形

MinerU 的内容列表决定块类型（正文、标题、公式、表、图/图注）、段落分组和阅读顺序。对应区域里只要还有原生文字，擦除框、`source_text` 和字号就锚定 PyMuPDF 的 span/字形，不用 MinerU 的粗 bbox 代替红区。跨框句子仍按 MinerU 段落合成一个逻辑段，再按框面积流回各物理框。

## 扫描页：OCR 文本直接进入来源清单

某一页或区域没有可抽取文字层，而 MinerU 给出了 OCR 文本和 bbox 时，用反算到页面坐标后的 bbox 和 OCR 文本新建 `source_map` 块（`origin: mineru-ocr`）。这样扫描件也进入同一条 `translator_v4.py` 流水线。该页原先的 `no-extractable-text` 待审项随之移除，改在 `extraction.warnings` 里说明来源是 OCR。

## 之后的步骤与本地提取相同

```powershell
python -X utf8 scripts/translator_v4.py plan --work work/paper --reviewed
python -X utf8 scripts/translator_v4.py translate --work work/paper
python -X utf8 scripts/translator_v4.py finish --work work/paper --translations work/paper/translations.json --reviewed --output outputs/paper.zh.pdf --font-dir C:/Windows/Fonts
```

字体、行距、逻辑段统一字号和验收标准见 [pdf-fonts.md](pdf-fonts.md) 与 [SKILL.md](../SKILL.md)。不要再调用 `reflow_reader.py finish`，也不要为双栏另装 Playwright。
