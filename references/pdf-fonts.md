# PDF 中文字体与溢出处理（V4.1）

本文只讲 PDF 原位重建的字体规则。HTML 阅读版的主题与内嵌字体见 [typography.md](typography.md)。

## 1. 按平台选字体

默认配置（`msyh.ttc` / `msyhbd.ttc` / `seguisym.ttf`）是 Windows 字体。换到 macOS / Linux 必须先配置本机字体，否则 finish 会在翻译后报缺字体——翻译前运行 `doctor` 可以直接拿到可粘贴的配置：

```powershell
python -X utf8 scripts/translator_v4.py doctor
```

| 平台 | 正文字体（regular） | 粗体（bold） | 符号备用（fallback，可选） |
|---|---|---|---|
| Windows | `msyh.ttc`（微软雅黑）或 `simsun.ttc`（宋体） | `msyhbd.ttc` 或 `simhei.ttf` | `seguisym.ttf` |
| macOS | `PingFang.ttc`、`Songti.ttc`（宋体）、`Hiragino Sans GB.ttc` | `PingFang.ttc`、`STHeiti Medium.ttc` | `Apple Symbols.ttf` |
| Linux | `NotoSansCJK-Regular.ttc`、`wqy-microhei.ttc`、`LXGWWenKai-Regular.ttf` | `NotoSansCJK-Bold.ttc`、`wqy-zenhei.ttc` | `DejaVuSans.ttf`、`NotoSansSymbols-Regular.ttf` |
| 开源推荐 | Noto Serif CJK SC（思源宋体，最贴近期刊原版） | Noto Sans CJK SC Bold | Noto Sans Symbols / DejaVu Sans |

配置写进 `config.yaml`（`doctor` 输出可直接粘贴；绝对路径、跨目录均可）：

```yaml
fonts:
  regular: C:/Windows/Fonts/msyh.ttc
  bold: C:/Windows/Fonts/msyhbd.ttc
  fallback: C:/Windows/Fonts/seguisym.ttf   # 可省略；缺失只告警不中断
```

规则：

- `regular` 必须存在；`bold` 缺文件时自动沿用 regular；`fallback` 只补希腊字母、上下标和科学符号，**缺失时降级为警告，不阻断流程**。
- 字体名可以是绝对路径，正文字体和符号字体可以放在不同目录。
- 字体必须覆盖本篇实际字符。`translate --translations … --font-dir …` 会按 ID 预检缺字并写入 `glyph-preflight.json`；`doctor --source paper.pdf` 在翻译前预检源文希腊字母/上下标。

## 2. 默认阅读样式与应用

默认中文 PDF 阅读样式如下；用户另有要求时以当次要求为准。

| 内容 | 默认样式 |
|---|---|
| 正文与方法 | 微软雅黑常规。目标字号 `min(9.5, max(8.5, round(source_font_pt * 1.05, 1)))`，即落在 **[8.5, 9.5] pt** |
| 摘要正文 | 同一自适应规则，上限仍为 9.5 pt；按内容及位置识别，不只依赖 `type=abstract`（该类型可能只是“摘要”标题） |
| 图题、表题 | 8.2 pt，仅“图 7”“表 1”等编号标签加粗；后续说明文字和 a、b、c、d 子图标记均用常规字重 |
| 关键词、作者单位 | 8 pt；全大写节标题（`1. INTRODUCTION`、`HIGHLIGHTS`、`KEYWORDS`）按标题翻译，不当成公式 |
| 标题与节标题 | 同家族粗体，维持层次；论文主标题沿用适合的较大字号，不套用正文 9.5 pt 上限 |
| 正文行距 | 从块上的 `leading` 起，放不下时收到不低于 `1.05` |

正文不再强制 9.5 pt 且 `min_scale: 1.0`。分类阶段把可译正文和摘要写成上述自适应字号，并把 `min_scale` 设为 `0.85`。宿主仍可在 `plan --reviewed` 前改 `source_map.json` 里的 `size`、`leading`、`min_scale`、`bold`；预检对正文会先收行距、再放到 0.85，成功则记 `adaptive-fit` 警告。

**同一段落只允许一套字号和行距。** 跨栏、跨页或由 MinerU/几何续段合并成一个逻辑段时，框 A 和框 B 最终渲染的 `font_size` 与 `leading` 必须相同。引擎按较紧的一框取统一值，再整段重排，不会出现半段 9.5 pt、半段 8.5 pt。

图题、表题的 caption 块不设置整块 `bold: true`；仅在译文开头用 `<b>图 编号</b>` 或 `<b>表 编号</b>` 包住标签（提交模板中的编号保护占位符保持原样），后续说明及 a、b、c、d 不放在加粗标签内。原生公式、图内标签、表格数值与英文书目保持原生保护，不全局强改字号或加粗。

已有任务改字号或加粗时复用译文及 ID 缓存，更新任务排版参数后重新审核冻结并 `finish`，不重新提取或全文重译。不要只手改会被下一次 `finish` 覆盖的 `layout.json`。交付前核对实际 PDF 的正文/摘要字号及图题、表题字重，不能仅凭配置值判断已生效。

放大后，跨页续句、独立引用、项目编号等短碎片优先利用同栏空白或与相邻文本合理合并，保留来源读序及保护边界；不把整个正文缩小来迁就一个短框。无空间时按下节处理真实阻断。正常留白和孤行不要求返工。

保护区继续做 120 DPI 比对，按主技能现有宽松分级处理；轻微差异记警告，不追求逐像素复刻。

## 3. 溢出处理顺序（不可跳步）

1. **调整段距、栏末换行与区域划分**——首选，不动字号。
2. **重划区域**——把溢出块按原栏宽重新切分。
3. **自动拟合**——正文先把 `leading` 收到不低于 `1.05`，仍放不下再把 `min_scale` 放到 `0.85`。成功后记 `adaptive-fit` 警告并正常交付，不把这次适配当成硬性 `overflow`。逻辑段落的每个物理框在拟合后被收成同一个 `font_size` 和 `leading`。孤立短行内片段或纯引用候选仍放不下时，可再放宽 `min_scale` 至 `0.75`。标题不套用微型块策略。实际参数保存在有效排版中，构建使用同一份参数。出版页脚通常原生保留。
4. **仅处理真实阻断**——仍无法完整写入或实际影响阅读时，定位相关 ID，保留可取得的诊断和预览。轻微差异不阻断、不要求返工；附译页或改变页序仍需用户授权。

不允许用大幅缩字、删译文、缩保护区来硬塞。缺字不得交付。

## 4. 已知字体细节

Windows 微软雅黑（`msyh.ttc`）的 ToUnicode CMap 会把间隔号 `·`（U+00B7）解码成 `∙`（U+2219）。验证器的 `normalize()` 已把两者视为等价，不需要为此改译文；若改用其他字体，命中该问题时先核对提取字符，再决定是否调整文本。相反，真正丢失的间隔号、电荷或单位仍会使审计失败。
