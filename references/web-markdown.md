# MD 与离线 HTML 阅读版导出

两种格式共用一个阅读清单（`manifest.json`），复用已核对的逐段翻译、原图与书目信息。脚本负责结构构建、安全过滤与排版渲染。

---

## 一、清单结构与规范

清单保存为 UTF-8 编码的 JSON 文件（例如 `reader.json` 或 `manifest.json`）：

```json
{
  "title": "论文中文标题",
  "scope": "前两页样张，附所引用书目",
  "expected_block_ids": ["S001", "F001"],
  "blocks": [
    {"id": "S001", "type": "paragraph", "source_page": 1,
     "source_text": "Original paragraph.",
     "translation": "完整中文译文。见[[fig:1|图 1]]和[[ref:1|文献 1]]。"},
    {"id": "F001", "type": "figure", "number": 1,
     "asset": "assets/figure1.png", "alt": "图 1",
     "translation": "图 1 的完整中文图注。"}
  ],
  "references": [{"number": 1, "text": "原英文书目信息。"}]
}
```

### 1. 字段说明
- **`meta` 论文元数据**：
  - `journal` / `article_type`：顶部彩色期刊与文章类型标头。
  - `title_en`：英文原标题（以学术斜体展示在中文标题下方）。
  - `authors`：作者列表，支持 `affiliations` 数组角标与 `corresponding` 通讯作者标识。
  - `affiliations` 与 `correspondence`：折叠展示于标题区下方，不干扰正文。
  - `doi` 与 `source_pdf`：自动生成权威链接与原版 PDF 对照下载跳转。
- **`blocks` 正文块**：
  - `type`：支持 `paragraph`（正文）、`heading`（章节标题，level 为 2–4）、`abstract`（摘要，以高阶卡片突出展示）、`note`（边注/说明）、`figure`（插图）、`equation`（公式裁图）、`table`（表格裁图）。
  - `asset`：图片相对路径，支持 PNG/JPEG/WebP。
  - `number`：图/表/式序号（figure 必填；无编号的 equation/table 可省略）。
  - `width_pt`：可选公式原版宽度（点），用于等比例控制公式裁图尺寸。
  - `pages` 或 `source_page`：原 PDF 页码，在段落工具中生成页码定位。
- **`references` 参考文献**：
  - 原编号数组，`number` 必须为正整数且唯一。保留完整英文题录。

### 2. 文本格式与引用语法
- **标准语法**：
  - 图引用：`[[fig:1|图 1]]` 或 `[[fig:1|图 1b]]`
  - 式引用：`[[eq:2]]`
  - 表引用：`[[table:1]]`
  - 文献引用：`[[ref:1]]`、`[[ref:1-3]]`、`[[ref:5,9-11]]`（渲染为原版上标格式）
- **自动兼容与保护**：
  - 原文提取的 `[Fig:1a]` 自动转为对应图跳转。
  - Unicode 上标、价态、单位指数和同位素一律原样保留，不自动猜测文献。需要跳转的文献先人工核对，再写显式 [[ref:N]] 标记。
  - 允许安全的学术上下标标签：`<sub>`、`<sup>`、`<strong>`、`<em>`。

---

## 二、导出命令与参数

```powershell
# 1. 导出标准学术宋体 HTML（使用系统字体栈）
python -X utf8 scripts/reader_export.py --manifest work/reader.json --format html --output outputs/html/reader.zh.html

# 2. 导出带子集化内嵌字体的单文件 HTML（可选，文件增量与效果取决于所选字体及内容）
python -X utf8 scripts/reader_export.py --manifest work/reader.json --format html --output outputs/html/reader.zh.html --embed-font serif=C:/Windows/Fonts/NotoSerifSC-VF.ttf

# 3. 导出文楷护眼主题 HTML
python -X utf8 scripts/reader_export.py --manifest work/reader.json --format html --output outputs/html/reader.kai.html --theme kai

# 4. 导出笔记友好 Markdown 格式（含 assets 资源包）
python -X utf8 scripts/reader_export.py --manifest work/reader.json --format md --output outputs/md/reader.zh.md

# 5. 运行导出回归测试
python -X utf8 scripts/test_reader_export.py
```

---

## 三、HTML 交互与功能特性
1. **真实学术上标与悬浮题录**：文献引用为原生上标 `<sup>`，鼠标悬浮立即可见文献详细信息；点击跳至文末，并激活右下角“↩ 返回刚才位置”多级返回。
2. **左侧目录导航**：文章大纲一目了然，滚动时根据阅读进度实时高亮当前小节。
3. **顶栏设置面板 (Aa 设置)**：
   - 字体切换：宋体（学术典雅）、楷体/文楷（纸书温润）、黑体（现代简洁）。
   - 字号缩放：`A-` / `A+` 动态无级调整。
   - 背景主题：纯白、米黄（复古护眼）、夜间深色（OLED 暗色）。
   - 栏宽调节：窄屏、居中（36rem 黄金阅读行宽）、宽屏。
4. **无缝中英对照**：默认不显示烦琐的英文标注，点击顶栏“中英对照”或段落旁“原文”按需展开对应英文段落。
5. **高清图片/公式灯箱**：点击图表或公式即可全屏放大查看细节，支持切换原图 100% 原始像素尺寸。
6. **阅读进度自动记忆**：再次打开时提示“上次读到这里：[章节名]”，一键平滑定位。

全文覆盖清单仍需独立准备；当前不自动把 PDF 区域清单转换为阅读清单。自动测试不代表所有阅读器兼容。meta.source_pdf 仅为外链，分享时需另附 PDF 并验证相对路径。
