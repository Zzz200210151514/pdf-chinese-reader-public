# 构建清单

环境需要 Python 3.10+、PyMuPDF 和含中文的本地字体。使用现有依赖；缺依赖时说明，不把联网翻译或提取设为硬要求。

```powershell
python -X utf8 scripts/pdf_reader.py inspect --source paper.pdf --output work/inspect --pages 1 2 3 4
python -X utf8 scripts/pdf_reader.py build --source paper.pdf --manifest work/layout.json --output outputs/chinese-sample.pdf --font-dir C:/Windows/Fonts
```

## prepare 与 merge：减少模型重抄英文

`prepare` 自动从 inspect 结果生成来源清单，预标记图内标签、旋转文字和页眉页脚；模型只输出 `{id: 中文}` 字典。

```powershell
python -X utf8 scripts/prepare_reader.py prepare --inspect work/inspect/source.json --output work/source_map.json
# prepare 会打印概览：总块数、translation_required 块数、各类预标数量、正文主字号、书目编号风格计数、drawing_clusters 是否可用。
# 核对 source_map 的读序、保留区域、书目和 targets，标记 layout_reviewed=true。
# 模型只写 translations.json：{"P01B001":"完整中文译文", ...}，不重抄英文和坐标。
python -X utf8 scripts/prepare_reader.py merge --map work/source_map.json --translations work/translations.json --output work/merged.json --layout work/layout.json
# 可选：同时输出 reader_export.py 用的清单（合并 paragraph_start=false 碎块，去掉保留块）
python -X utf8 scripts/prepare_reader.py merge --map work/source_map.json --translations work/translations.json --output work/merged.json --reader work/reader.json
python -X utf8 scripts/translation_audit.py --map work/merged.json --output work/audit.json
python -X utf8 scripts/pdf_reader.py preflight --source paper.pdf --manifest work/layout.json --output work/fit.json --font-dir C:/Windows/Fonts
```

### 预标记规则

`prepare` 的 `preserved` 统计五类，`prepare_summary.preserved_ids` 列出每类的块 ID：
- **`rotated-text`**：任一行 `dir` 不是 `(1,0)`（如纵轴标题，提取出来是乱序字符）。
- **`figure-label-candidate`**：与正文段落不重叠，并满足下列任一条：
  - 全部由数字或单个字母组成（刻度 `0.3`、`1.3 1.4 1.5`，面板字母 `a b`）；
  - 不超过 60 个字符，且落在矢量图形簇或图片块向外 20pt 的范围内（`OER region`、图例）；
  - 不超过 40 个字符，且主字号低于正文主字号的 80%。
- **`running-header-footer`**：把数字归一后，在至少 3 页且至少一半页面的相同高度重复出现（页眉、页脚、页码行）；抽样页数少于 3 时不触发。
- **`bibliography`**：出现 `References`/`Bibliography` 标题行之后，含有从 1 起连续编号条目的块。编号接不上的块不预标，所以书目后面的致谢、作者贡献等仍需翻译；作者-年份制书目和跨栏续行不在此列，需人工标。预标书目只是不送翻译，完整书目仍要核对后填进 `references`。
- **`non-prose-candidate`**：与正文段落不重叠，并且没有 3 个字母以上的单词、或字母占比低于三分之一、或是不超过 60 个字符的带 `=`/`×`/式号的短行（独立公式、式号、数值行）。多行长公式不在此列。

`acronym_candidates` 列出正文里首次出现的 `全称 (缩写)` 定义（缩写、英文全称、所在块），用来起草 `terminology_ledger`；它只是线索，译名仍需核对后再写进术语表。

预标只是候选，需核对：误标的块设回 `translation_required=true` 并删除 `preservation_reason`，之后 `merge` 才接受它的译文。与正文段落重叠的上下标碎片（如 `18O.`）不预标。公式、表格数据和书目的预标并不完整，漏掉的仍需人工标保留。矢量图规则依赖 inspect 导出的 `drawing_clusters`；旧的 `source.json` 没有这一项时概览会提示，重跑 `pdf_reader.py inspect` 即可。

### 来源清单与合并

准备脚本接受 `pdf_reader.py inspect` 的 JSON；MinerU Markdown 仅辅助读序，不直接推算坐标。文本块不是完整语义段落：翻译时将跨页碎块合并理解，输出仍按原 ID 分配，不重新抄英文。`prepare` 从每行字号统计正文主字号、为每块计算 `source_text_sha256`、提取图片保护区和书目候选，并写出 `expected_block_ids`。

人工核对后将页眉、原公式、表格数据及原书目设为 `translation_required=false` 并填写 `preservation_reason`；预标记仅为建议，不能静默丢弃正文。`reference_candidates` 按 `bracket`/`dot`/`plain`/`parenthesis` 四种风格提供首行候选，实际书目需另核对正文引用与完整书目，建立明确 `targets`；所有保护区以原 PDF 为准。

`merge` 严格检查：拒绝重复 JSON 键、未知 ID、缺译、对预标记块的译文及无理由的保留块。生成 PDF 排版清单时，相同左右边界的重叠区域自动合并，其余冲突一次性报出；化学碎块等不应加段落换行时，在后续块设 `paragraph_start=false`。完整来源清单与全部 ID 保留在 merged.json；不改变源文本来消除审计报警。`--reader` 另存一份给 `reader_export.py` 的阅读清单：需先在来源清单里填好中文 `title`，`paragraph_start=false` 的碎块并入前一块，保留块不进正文而列在 `omitted_preserved_ids`；图、公式和表格的裁图块仍需人工补充。

`build` 自动做同样的预检；独立 `preflight` 用于调版，正式构建会重新检查防止使用过期结果。错误报告不截断到第一个问题，字号仅为建议，不能绕过用户最小字号或靠删正文适配。

页号为原 PDF 的 **1 起始**页号；目标页默认以选定页映射到输出。rect 为 PDF 点坐标 `[x0,y0,x1,y1]`。文本区域互不重叠，不能覆盖保护区域。

### 版式与字体约定

`layout_mode` 为实际构建模式，`requested_layout` 为用户要求，均默认 `source`（原位排版）；附译页为 `appendix`。两者不一致时，只有用户已明确授权的 `layout_change_authorized: true` 才能通过。字段记录实际授权，不能由脚本冲突或一次像素失败推断。`reference_layout` 可记录用户提供的版式样例；样例仍需人工比较，不是自动视觉验收。

`fonts.fallback` 可选，主字体缺少的字符会显式使用备用字体；主、粗体与备用字体均不含该字时预检失败。检查包含上下标、希腊字母及科学运算符。PDF HTML 的段落与正文外边距固定为零，段距须在布局中明确控制。

### 默认参数

`prepare` 生成的块与手写清单用两套默认值，常量在 `prepare_reader.py` 顶部。`prepare`：`size` 取该块按字符数加权的主字号（同时记入 `source_font_pt`），`leading` 1.17，`min_scale` 0.88（允许缩到 88%）；块内文字全为粗体时设 `bold=true`，标题因此沿用原字号和粗体。手写清单省略这些字段时，`pdf_reader.py` 用 9.35、1.35、1（不缩小）。

译文里的上下标可写 `<sub>`/`<sup>` 或 `^{2}`/`_{2}`，另放行 `b/i/strong/em`，其余标签按文本转义。`[[ref:N]]` 的目标来自来源清单的 `references`（核对后填写，`layout` 原样传给排版清单）或手工 `targets`。

```json
{
  "title": "中文论文标题｜样张",
  "scope": "前两页中文正文与参考文献",
  "layout_mode": "source",
  "requested_layout": "source",
  "fonts": {
    "regular": "simsun.ttc",
    "bold": "msyhbd.ttc",
    "fallback": "seguisym.ttf"
  },
  "pages": [
    {
      "source_page": 1,
      "redactions": [
        [
          40,
          530,
          295,
          692
        ]
      ],
      "regions": [
        {
          "id": "intro",
          "rect": [
            40,
            530,
            295,
            692
          ],
          "size": 9.35,
          "html": "完整译文<sup><a href=\"https://pdf-reader.invalid/ref/1\">1</a></sup>。见<a href=\"https://pdf-reader.invalid/fig/1\">图 1</a>。"
        }
      ],
      "protected": []
    },
    {
      "source_page": 2,
      "redactions": [],
      "regions": [],
      "protected": [
        {
          "id": "fig1",
          "rect": [
            56,
            45,
            544,
            552
          ]
        }
      ]
    }
  ],
  "targets": {
    "fig/1": {
      "source_page": 2,
      "point": [
        40,
        40
      ]
    }
  },
  "references": [
    {
      "number": 1,
      "text": "作者、原英文题名、期刊、卷页、年份。"
    }
  ]
}
```

- `regions`：`id` 每个页面内唯一；完整译文 HTML，允许 `sub/sup/b/strong/br` 和受控链接；`size` 默认9.35，`leading` 默认1.35；`min_scale` 默认1（不缩小）；`bold` 默认false。分割一段时必须另存段落映射，保证完整覆盖。
- `redactions`：明确选定移除的英文文字区域，脚本保留图片和线条。清单不能靠自动栏识别猜选区。
- `protected`：图、表、公式原貌区域；脚本以120dpi做像素一致核查。需要改变的图内标签不要声称受保护。
- `overlays`：仅经人工核对需白色覆盖的扫描文字、小栅格日期或正文行内符号遗留的矢量笔画，如 `{"rect":[39,266,174,282]}`；不能覆盖保护区域。
- `targets`：如 `fig/1`、`eq/2` 对应原页号及坐标。清单中的引用必须有目标；未包含的图不要创建临时链接。
- `references`：按原编号排列；编号需为正整数且唯一，全文需另核对书目完整性，书目原信息。脚本附录分页并建立 `ref/N` 目标。未配置此项时，需在 `targets` 手工指定所有 `ref/N` 目标。
- 外部 DOI URL 作为外链保留；`https://pdf-reader.invalid/类型/编号` 仅为内部链接占位标记，输出不得残留。

输出包括 PDF、同名 `.preflight.json`、`.validation.json`、`.overview.pdf` 全页拼图和 `.previews/` 逐页预览。`reference_count` 统计附录书目与手工 `ref/N` 目标的并集，`appended_reference_count` 仅统计新附录条目；数量不认证书目完整性。`layout_contract` 记录版式选择，`scientific_symbols_complete` 检查译文科学符号在输出文字层中是否保留。验证记录包含区域中文字符覆盖、缩放、保护区域像素和链接目标，另有两项只供复核、不会让构建失败的信号：`english_residue` 列出输出正文页里既没译成中文、也不在保护区内的英文段（10 个以上英文单词），多半是漏翻或漏标；`outside_region_changes` 列出把所有文字区域遮掉后、72 dpi 下仍与原页有 0.1% 以上像素差异的页，提示 redaction 可能误伤了未声明保护的图。两项为空不代表译文或版面无误；不宣称自动验收译文质量或实际阅读器点击。脚本不会翻译文献、自动拆原页或导入 Zotero。

## 返回与导航

脚本会给有正文引用的书目条目编号添加“返回首次引用”链接。该功能是预设目的地，使用说明应写清楚。图下返回按钮可在安全空白区域作为一个 `regions` HTML 链接添加，例如 `https://pdf-reader.invalid/return/fig/1`，并在 `targets` 中把 `return/fig/1` 映射到原文首次引用的页与坐标。不要占用图或公式保护区域。其他自定义导航也按同样方式配置；最终需视觉查看按钮是否可见，不能只创建空点击区。
