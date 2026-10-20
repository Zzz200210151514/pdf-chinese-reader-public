# pdf-chinese-reader

将英文学术论文完整翻译为中文 PDF、MD 或离线精美 HTML 阅读版，复用同一份译文、图表和参考文献。默认单任务串行、本地提取、重点校读；开始时只确认尚未明确的格式与提取方式。

不主动询问或推荐并发；用户明确要求时才按 [并发执行](references/workflow.md#并发执行) 分工。模型只读取当前待译包和异常片段，复用已合格译文；默认不做全文第二遍对照。微型块轻微溢出自动拟合，排版警告直接交付；只有内容缺失或实际难读才局部修正，不自动扩展为引擎重构，见 [返工边界](SKILL.md#检查与返工边界)。

- **PDF**：尽量保持原版双栏、高清原图、公式和页码。
- **MD**：正文和 assets 文件夹配套分享，笔记阅读友好。
- **HTML**：单文件离线阅读，内嵌图片；支持上标引用、悬浮预览、动态返回、中英对照、图片放大和进度恢复。可选字体嵌入，效果和文件增量需按实际内容与浏览器验证。

MD/HTML 生成入口及清单规范见 `references/web-markdown.md`；字体与排版美化指南见 `references/typography.md`。PDF 默认原位替换正文，并以用户提供的成品为版式基准。中文正文默认 9.5 pt、摘要 9 pt、图题与表题 8.2 pt；仅“图 7”“表 1”等编号标签加粗，后续说明及 a、b、c、d 不加粗。行距从 1.24 起，溢出优先调整区域和行距。实际设置方法见 [字体与拟合](references/pdf-fonts.md#2-默认阅读样式与应用)。局部冲突先修复区域与保护边界；未经用户明确要求或授权，不得改为“原文页 + 中文译页”。附译页约定见 `references/pdf-appendix-translation.md`。

新 PDF 任务按 [下一篇使用说明](references/v41/下一篇论文使用说明.md) 执行 `prepare → 审核/plan → translate → finish`；`prepare` 生成来源清单与审查项，审核冻结后才派发翻译，`finish` 校验覆盖、原位重建和功能检查。默认 `provider: host`，不会自动调用付费 API，也不会自行完成全文翻译。模型、领域与路由由配置决定；已有旧来源清单、ID和 `prepare_reader.py prepare/merge`、`pdf_reader.py build` 入口继续兼容，不必强制迁移。

默认采用重点校读，全文第二遍对照可选精校档；自动检查不能认证翻译准确性。并行翻译仅在用户明确要求时启用，耗时与 Token 分别实测，不承诺固定速度。预标记仅为建议，可撤销；扫描页、复杂重叠或无法可靠提取的内容应明确复核，不承诺任意 PDF 自动成功。书目区预标、公式行预标、缩写定义线索、英文残留扫描和区域外像素对比这几项机制参考了 [translate-academic-pdf](https://github.com/FertayLageeze/translate-academic-pdf)（MIT），实现为本技能自写。

回归检查覆盖来源清单、提取审计、MD/HTML 导出、矢量颜色、版式授权、字体缺字与原位书目链接。自动检查不能代替原文校读、版面查看或阅读器点击测试。

---

## 快速开始

这是给 AI 编程助手使用的技能包。脚本负责提取、排版和检查；全文翻译由你当前使用的宿主模型完成。默认不调用付费翻译 API。

1. 下载本仓库，将整个文件夹命名为 `pdf-chinese-reader`，放入你的 skills 目录。Codex 默认位置为 `~/.codex/skills/pdf-chinese-reader/`；若设置了 `CODEX_HOME`，则放在其 `skills/` 下。
2. 在技能目录安装依赖并检查环境：

   ```text
   python -m pip install -r requirements.txt
   python -X utf8 scripts/translator_v4.py doctor
   ```

   Python 需要 3.10 或更高版本。Windows 默认探测微软雅黑；macOS/Linux 按 `doctor` 输出配置本地中文字体。字体文件不包含在仓库中。
3. 让助手加载技能并提供论文：

   ```text
   使用 $pdf-chinese-reader 把这篇论文做成中文 PDF 阅读版。
   正文 9.5 pt、摘要 9 pt；图表题只加粗编号标签，后面说明不加粗。
   先试前 3 页，保留原图、公式和引用跳转。
   ```

完整使用命令见下文及 [下一篇论文使用说明](references/v41/下一篇论文使用说明.md)。只运行 `prepare` 不会自动得到译文；宿主需消费待译包并提交译文后再 `finish`。

## 公开分享范围

- 包含技能、脚本、术语库、文档及可本地生成的合成测试；不包含真实论文、译文、账户凭据、字体文件或运行缓存。
- 默认本地提取、串行处理；需要 MinerU 上传时先取得用户授权，远程模型需显式配置。
- 自动检查验证完整性、布局及链接等机械条件，不能认证翻译语义准确性。复杂版式仍可能需要局部人工处理。
- 本次未另行指定项目源码许可证；第三方依赖及参考项目遵循其各自许可。参考来源见下文，未打包其第三方代码或真实论文。

## 安装到 Codex / Antigravity

将整个仓库文件夹重命名为 `pdf-chinese-reader`，放入对应 skills 目录（根目录应直接包含 `SKILL.md`）：
- Codex: `$CODEX_HOME/skills/pdf-chinese-reader/`（未设置时为 `~/.codex/skills/pdf-chinese-reader/`）
- 其他宿主：放入该宿主配置的 skills 目录，保留整个技能文件夹。

调用示例：

```text
使用 $pdf-chinese-reader 把这篇英文论文做成中文 HTML 阅读版，保留原版图片和公式，先做前四页，我检查排版和图文跳转。
```

---

## MinerU 法：双栏中文重排

原位法仍为默认。说“用 MINERU法”时，用独立的 `reflow_reader.py` 提取结构并重排中文，复用原图、公式和译文缓存。默认微软雅黑、正文9.5pt、摘要9pt、图表题8.2pt；保留双栏节奏，允许自然换页。参考原稿保留刊名、出版标识、首页分区和页眉，位置大致接近即可；期刊样式需按实际原稿配置，不是任意期刊自动复刻。

正文文献、图表及公式引用使用清晰蓝色；文献前的 `[N]` 编号本身可返回正文。PDF返回首次引用，离线HTML可返回最近阅读位置；子图按核实的坐标跳转，落点以最终PDF实际内容为准。

双栏排版另需本地浏览器依赖：

```text
python -m pip install playwright
python -m playwright install chromium
```

MinerU上传须已有授权；复用成功的结构化JSON时不用重新上传。完整流程、完整段落翻译、期刊布局要求及验收见 [MinerU 法](references/mineru-method.md)。它与 `translator_v4.py --extractor mineru` 的原位模式分别使用。

## 本地脚本依赖

Python 3.10+。
- 基础依赖：PyMuPDF（PDF 提取与排版）、PyYAML（V4 配置和长期术语资产）。PDF 输出另需本地中文字体。
- 可选依赖：`fonttools` 与 `brotli`（用于 HTML 字体子集化内嵌 `--embed-font`，免依赖系统字体）。

命令示例以 Windows / PowerShell 书写（`C:/Windows/Fonts` 等）。macOS / Linux 上字体路径不同，跑 `translator_v4.py doctor` 生成本机 `fonts:` 配置；MinerU 凭据在非 Windows 系统用环境变量 `MINERU_TOKEN` 提供。

```powershell
python -m pip install -r requirements.txt
```

### 运行命令示例

V4 命令在实际安装的技能目录运行。`prepare` 默认本地提取，生成空 jobs 与结构预检；实际处理 `review.json` 和 `source_map.json` 的待审项后，运行 `plan --reviewed` 冻结来源，再运行 `translate` 由宿主消费 `pending-jobs.json`，生成仅含需翻译 ID 的 `translations.json`。`--reviewed` 是已完成审核的记录，不能用于绕过未解决的来源或几何问题。

```powershell
# 可选预检：依赖、字体探测，输出可粘贴的 fonts: 配置
python -X utf8 scripts/translator_v4.py doctor --source paper.pdf

python -X utf8 scripts/translator_v4.py prepare --source paper.pdf --work work/paper --extractor local
# 查看当前阶段与下一步；逐条记录待审项处理结论（自动重新 plan）
python -X utf8 scripts/translator_v4.py status --work work/paper
python -X utf8 scripts/translator_v4.py resolve --work work/paper --issue-key "<id|check|page>" --note "<handling>"
python -X utf8 scripts/translator_v4.py plan --work work/paper --reviewed
python -X utf8 scripts/translator_v4.py translate --work work/paper
# 宿主串行读取当前待译包，重点校读并保存 {id: 中文}；不整份打印私有校验字段
# 提交译文批次时带 --font-dir 可做按 ID 缺字预检
python -X utf8 scripts/translator_v4.py translate --work work/paper --translations batch.json --font-dir C:/Windows/Fonts
# --source 可省略：默认使用 prepare 保存的 work/paper/source.pdf 快照
python -X utf8 scripts/translator_v4.py finish --work work/paper --translations work/paper/translations.json --reviewed --output outputs/paper.zh.pdf --font-dir C:/Windows/Fonts
```

只在用户已经选择 MinerU 时给 prepare 使用 `--extractor mineru`，可选 `--mineru-cli`。两条命令均支持 `--config`；远程 provider 必须由用户明确配置，可能产生外部服务费用。详见 [宿主交接和 API](references/v4.md)。默认书目在原页，引用目录另存 `bibliography`；只有明确授权追加书目才设置 `explicitappendrefs=true`。

```powershell
# 1. 导出高质感 HTML 阅读版（推荐内嵌思源宋体，跨平台效果需实测）
python -X utf8 scripts/reader_export.py --manifest work/manifest.json --format html --output outputs/paper.zh.html --embed-font serif=C:/Windows/Fonts/NotoSerifSC-VF.ttf

# 2. 导出文楷护眼主题 HTML
python -X utf8 scripts/reader_export.py --manifest work/manifest.json --format html --output outputs/paper.kai.html --theme kai

# 3. 导出 Markdown 版本
python -X utf8 scripts/reader_export.py --manifest work/manifest.json --format md --output outputs/paper.zh.md

# 4. 构建双栏中文 PDF
python -X utf8 scripts/pdf_reader.py build --source paper.pdf --manifest work/layout.json --output outputs/paper.zh.pdf --font-dir C:/Windows/Fonts

# 仅在用户要求或授权附译页，并在清单记录版式选择后使用
python -X utf8 scripts/append_translation_pdf.py --source paper.pdf --manifest work/append-translation.json --output outputs/paper.zh.pdf --font C:/Windows/Fonts/msyh.ttc
```

---

## 字体与验证

HTML 提供宋体、文楷与无衬线主题、内嵌图片、中英对照与动态返回。PDF 沿用原页布局与已确认的字体风格，内部链接支持固定目标与返回首次引用；阅读器历史负责返回刚才位置。

PDF 预检主字体与粗体字体的字符覆盖，可用 `fonts.fallback` 补齐科学符号。构建检查溢出、保护区像素、科学符号文字层和链接目标；抗锯齿级像素差异按警告报告，不阻断交付。默认通过功能检查即可交付，必要时快速看拼图和真实异常；不强制逐页巡视。按平台的字体选型和溢出处理顺序见 [pdf-fonts.md](references/pdf-fonts.md)，`translator_v4.py doctor` 可在本机直接生成可粘贴的 `fonts:` 配置。

以下仅用于明确的脚本维护任务；普通论文使用流程自带门禁，不重复跑开发回归。在技能目录运行：

```powershell
python -X utf8 scripts/test_prepare_reader.py
python -X utf8 scripts/test_extraction_audit.py
python -X utf8 scripts/test_reader_export.py
python -X utf8 scripts/test_pdf_colors.py
python -X utf8 scripts/test_pdf_layout.py
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

本次真实外部样本的原位重建回归已通过：Han 9 页、Monat 15 页、Pan 12 页；自动分类分别得到完整的 42、8、75 条编号书目。Ma2023（14 页、151 个待译块）与 Wang2023（12 页、151 个待译块）已从 prepare 到 finish 全流程通过，英文残留 0；它们验证已有重建路径及来源结构，不能等同于新的全文译文质量认证或所有阅读器点击验收。合成样本单独覆盖单/双栏、表格、公式与矢量图；远程适配器测试使用模拟响应，不表示真实付费服务已经联调成功。Chen2019 已完成 12 页交付；V4.1 重建回归与仍未达标的陌生样本边界见 [验证说明](references/v41/回归测试与质量验证.md)。真实 PDF、译文与提取缓存不入仓库。

## V4.1 定向优化

[修改说明](<references/v41/V4.1 通用优化说明.md>) · [实测性能](references/v41/性能前后对照.md) · [质量与未测试范围](references/v41/回归测试与质量验证.md) · [下一篇使用说明](references/v41/下一篇论文使用说明.md)。V4.1 保留原 writer，前移源结构门禁、增加逐块恢复与有界并发、统一字体及 workflow 日志。陌生论文冷启动严格验收尚未达标，不能将缓存回归耗时称作新论文全文翻译耗时。
