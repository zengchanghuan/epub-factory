# 一站式排版服务 — 功能规划与现状

> 口号：**一站式排版服务**  
> 目标：从 Word/Markdown 到高标准 EPUB 的定制转换

**说明**：本文档仅做规划与现状标注，不涉及实现。标注含义：
- **已有**：当前代码已实现并可用的能力
- **部分已有**：有相关能力但未完全覆盖该条
- **未实现**：当前无此能力
- **难度**：低 / 中 / 高（实现与维护成本）
- **建议**：✅ 建议做 / ⚠️ 谨慎或延后 / ❌ 不建议做（含原因）

---

## 1. 核心排版引擎 (Core Typography)

| 功能项 | 现状 | 难度 | 建议 | 备注 |
|--------|------|------|------|------|
| **行间距 / 段落间距 / 字间距优化** | 部分已有 | 中 | ✅ | 当前 CssSanitizer 会移除部分内联 `line-height`；PDF→EPUB 模板里有 `line-height: 1.8`。可做：在 TypographyEnhancer 或独立 Cleaner 中注入/归一化 `line-height`、`margin`、`letter-spacing`，需兼顾不同阅读器兼容性。 |
| **首字下沉 (Drop Caps)** | 未实现 | 中 | ✅ | 用 CSS `initial-letter` 或 `::first-letter` 针对章节首段。需约定选择器（如 `.chapter > p:first-of-type`），并注意阅读器支持度（initial-letter 部分设备不支持，可做 fallback）。 |
| **字体嵌入 (OTF/TTF)** | 未实现 | 中 | ✅ | 需：将字体文件打入 EPUB、在 CSS 中写 `@font-face`、可能做子集化以控制体积。与「高标准 EPUB」强相关，建议做。 |
| **分页逻辑 (page-break-before)** | 未实现 | 低 | ✅ | 章节级 CSS 注入 `page-break-before: always`（或 `break-before: page`）。实现简单，阅读器支持较好。 |
| **繁简 / 横竖转换** | **已有** | — | — | **已有**：CjkNormalizer 提供竖排→横排（writing-mode、竖排标点替换）、繁→简（OpenCC）。输出模式为「横排简体/横排繁体」。 |
| **横排→竖排 (LTR→RTL)** | 未实现 | 高 | ⚠️ | 与当前主流程「竖转横」相反。需处理 writing-mode、标点旋转、阅读顺序。需求明确再做，否则易与现有逻辑纠缠。**繁体竖排另有痛点**：标点需从简体横排的弯引号 `""` 改为直角引号 `「」`，见 §1.2。 |

**补充建议**：
- 增加「排版预设」：如「文学版」「学术版」，对应不同 line-height / 段间距 / 是否首字下沉，便于产品化。
- 竖排若做，建议单独开关或单独 pipeline，与现有横排主路径解耦。

### 1.1 核心排版 — 细规格（仅规划）

| 子项 | 输入 | 输出/行为 | 参数/开关 | 与现有模块衔接 |
|------|------|-----------|-----------|----------------|
| **行/段/字间距** | 当前 pipeline 的 CSS(2) 与 HTML(9) | 注入或覆盖：`line-height`（如 1.6/1.8）、`margin-block` 段间距、`letter-spacing`（可选）。不破坏已有内联 style 中需保留的项。 | 预设枚举：`literary` / `academic` / `custom`；custom 时接收数值或「不修改」。 | 放在 TypographyEnhancer 之后或合并进 TypographyEnhancer；仅处理 item_type=2 的 CSS 与可选的一处「全局注入」HTML 的 `<style>`。 |
| **首字下沉** | 章节首段（需约定选择器） | 为首段首字应用 `initial-letter` 或 `::first-letter` 样式；不支持的阅读器 fallback 为普通段落。 | 开关 `drop_caps: bool`；可选 `selector`（如 `.chapter > p:first-of-type`）。 | 独立 Cleaner 或并入 TypographyEnhancer；仅处理 HTML，可依赖现有 class 或注入 class。 |
| **字体嵌入** | 用户上传 OTF/TTF 或预设字体 ID | 字体文件打入 EPUB（如 OEBPS/fonts/），在全局或 per-chapter CSS 中写 `@font-face`，并在 body 或指定 class 上设置 `font-family`。 | `embed_fonts: List[path_or_id]`；可选 `apply_to: "body" | "class"` 及 class 名。 | 在 Unpack/打包前增加「字体注入」步骤；需写 manifest 与 MIME；与 DeviceProfile 无冲突。 |
| **分页** | 章节对应 XHTML | 在每章根容器（或 body 下首个子元素）上注入 `page-break-before: always`（或 `break-before: page`）。 | 开关 `chapter_page_break: bool`（默认 true）。 | 在 CSS 注入阶段或独立 Cleaner；需能识别「章节」边界（与 TocRebuilder 的章节概念一致即可）。 |

**验收要点**：输出 EPUB 经 epubcheck 通过；在 1–2 个主流阅读器内目视确认行距/首字/字体/分页符合预期。

### 1.2 繁体竖排标点修正（痛点规格）

繁体竖排时，**标点符号必须与横排区分**：简体横排常用弯引号 `“”`，繁体竖排应改用直角引号 `「」`，并需考虑标点旋转角度以适配竖排排版。

| 项 | 规格 |
|----|------|
| **替换规则** | 横排弯引号 `“` `”` → 竖排直角引号 `「` `」`；可扩展：`‘’` → `『』` 等。按段落或全文在转换格式时统一替换。 |
| **实现方式** | 在竖排 pipeline 中增加一步：用正则或 DOM 遍历识别引号及可选的其他标点，按规则替换；若竖排需标点旋转，可在同一流程中为对应节点注入 `text-orientation` / 专用 class。 |
| **脚本形态** | 可做成独立 Python 脚本：读入 HTML/EPUB → 正则替换标点（及可选的旋转标记）→ 写出。与格式转换（横竖/繁简）同流程执行，保证「一次转换、标点一致」。 |
| **衔接** | 若做竖排输出：在 CjkNormalizer 或竖排专用分支中调用；与现有「竖转横」路径解耦，避免误改横排内容。 |

### 1.3 多端适配层 (Multi-device adaptation)

编写或注入 CSS 时需考虑不同设备与系统主题，避免在不同亮度与阅读器下排版崩坏。

| 功能项 | 现状 | 难度 | 建议 | 备注 |
|--------|------|------|------|------|
| **多端适配层** | 未实现 | 中 | ✅ | 全局或章节 CSS 中需包含：`@media (prefers-color-scheme: dark/light)` 下的颜色与对比度适配，确保深色/浅色下文字与背景可读；针对 **Kindle 渲染引擎** 的特定 Hack（如 -webkit- 前缀、已知的布局兼容写法），保证在 Kindle 上排版不崩坏。 |

**细规格（仅规划）**：

| 子项 | 输入 | 输出/行为 | 与现有模块衔接 |
|------|------|-----------|----------------|
| **prefers-color-scheme** | 当前注入的 CSS | 在全局或 base 样式中增加 `@media (prefers-color-scheme: dark)` 与 `light` 分支，设定 `color`/`background` 等，避免纯黑/纯白导致不可读。 | 在 TypographyEnhancer 或「多端适配」Cleaner 中注入；不覆盖用户已设的字体/行距。 |
| **Kindle 渲染 Hack** | 同上 | 针对 Kindle 已知问题：如某些 flex/grid 不支持、字体回退、页边距等，通过条件注释或单独一段「Kindle 用」CSS 或 -webkit- 前缀修复。可维护一份「Kindle 兼容清单」随版本更新。 | 与 DeviceProfileCompiler 或独立「设备 CSS 注入」步骤结合；输出 EPUB 内可含一份 `kindle.css` 或在内联 style 中按需引入。 |

**验收要点**：在深色/浅色系统下打开 EPUB，排版不崩、可读；在 Kindle 或 Kindle 模拟器上打开，无严重错位或重叠。

---

## 2. 内容解析与清理 (Data Sanitization)

| 功能项 | 现状 | 难度 | 建议 | 备注 |
|--------|------|------|------|------|
| **代码瘦身（冗余 span / 内联样式）** | 部分已有 | 中 | ✅ | **部分已有**：CssSanitizer 移除 font-family、line-height(px)、background-color 等内联。可增强：合并/删除无样式 span、清理 Word/Pandoc 产生的多余 class。 |
| **结构化校验 (EPUB 3.0 + epubcheck)** | **已有** | — | — | **已有**：compiler 内 _run_epubcheck()，通过后才标 completed，否则 failed。 |
| **多源输入：.docx / .md → XHTML** | 未实现 | 高 | ✅ | 当前仅支持 .epub / .pdf 输入。Word/MD 需：docx→HTML（python-docx 或 pandoc）、MD→HTML（markdown 库或 pandoc），再走现有清洗+打包。建议用 pandoc 做统一入口，再接入现有 pipeline。 |

**补充建议**：
- 明确「高标准」的 XHTML 规范：如只用语义化标签、禁止内联样式（或白名单），便于清洗器有据可依。
- 若上 Word/MD：先支持 .md（实现成本低于 .docx），再补 .docx。

### 2.1 多源输入与代码瘦身 — 细规格（仅规划）

**2.1.1 高标准 XHTML 规范（建议先定再实现）**

| 维度 | 规则建议 | 用途 |
|------|-----------|------|
| 标签 | 优先使用语义化标签（section, article, h1–h6, p, ul/ol/li, blockquote, figure/figcaption）；禁止无意义 div/span 嵌套。 | 清洗器可据此做「瘦身」与替换。 |
| 内联样式 | 禁止或白名单：仅允许必要项（如 span 内 color 用于强调）；其余移至 class + 全局/章节 CSS。 | 与现有 CssSanitizer 一致，可扩展白名单。 |
| 属性 | 保留 id（锚点）、必要 aria；移除空 class、重复 class。 | 便于 TOC 与可访问性。 |

**2.1.2 Markdown → XHTML 入口**

| 项 | 规格 |
|----|------|
| 输入 | 单文件 .md 或目录（多 .md 合并顺序由清单或文件名排序约定）。 |
| 转换 | 使用 Pandoc 或 Python markdown 库输出 XHTML 片段；需生成符合 EPUB 的壳（html/head/body、charset、可选的 base 样式）。 |
| 输出 | 与现有 pipeline 一致：一组 XHTML 项 + 可选的单 CSS；不直接输出 EPUB，而是「内存或临时目录中的 book 结构」，再交给现有 Cleaner 链 + TocRebuilder + Packager。 |
| 衔接 | 新增「输入适配层」：对 .md 调用 Pandoc/库 → 得到 book 或 (items, spine)，再调用现有 ExtremeCompiler 或等价清洗+打包路径；PDF 入口可类比（当前已有 PDF→EPUB 再进 pipeline）。 |
| 依赖选型 | Pandoc（推荐）：支持 TOC、公式、代码块、多格式；Python 库：markdown + extensions（表格、代码高亮等），控制力强但需自建 TOC/公式。 |

**2.1.3 Word (.docx) → XHTML 入口**

| 项 | 规格 |
|----|------|
| 输入 | 单文件 .docx。 |
| 转换 | python-docx 提取段落/标题/表 → 自建 XHTML；或 Pandoc docx→html 再后处理。 |
| 输出/衔接 | 与 2.1.2 同：产出 book 或 (items, spine) 后进现有 pipeline。 |
| 依赖选型 | Pandoc 优先（格式全）；python-docx 仅当需精细控制段落样式时考虑。 |

**2.1.4 代码瘦身增强（在现有 CssSanitizer 基础上）**

| 规则 | 说明 |
|------|------|
| 合并/删除无样式 span | 遍历 DOM：若 `<span>` 无 class 且无 style（或 style 为空），用其文本节点替换自身。 |
| 清理 Word/Pandoc 冗余 class | 可配置「保留的 class 前缀」或白名单；其余删除。如 `MsoNormal`、`footnote` 等按需保留或映射为自有 class。 |
| 验收 | 瘦身后 HTML 体积下降、epubcheck 仍过；目视无版式错乱。 |

---

## 3. 学术与技术增强 (Academic Features)

| 功能项 | 现状 | 难度 | 建议 | 备注 |
|--------|------|------|------|------|
| **数学公式 LaTeX → SVG** | 未实现 | 高 | ✅ | 需选型：如 latex2svg、MathJax 服务端渲染、或 pandoc math 输出。严禁低清位图，SVG 需保留可访问性（可考虑 aria-label / MathML 双输出）。 |
| **代码高亮（Markdown 代码块→HTML+CSS）** | 未实现 | 中 | ✅ | 若先上 MD 输入，可用 pygments 或 highlight.js 服务端生成带 class 的 HTML，并注入一套阅读器可用的高亮 CSS。 |
| **多级目录 (NCX/Nav)** | **已有** | — | — | **已有**：TocRebuilder 扫描 h1–h6 与加粗居中段落，生成 toc.ncx + nav.xhtml，并注入锚点。 |
| **正文内交叉引用 (Cross-reference)** | 部分已有 | 中 | ✅ | TOC 锚点已有；正文内「见第 X 章」「图 1-2」等自动跳转需解析引用并写 `<a href="...#anchor">`。可放在后处理 Cleaner 或与 TOC 同层。 |
| **弹出式注释 (epub:type="noteref")** | 未实现 | 高 | ⚠️ | EPUB 3 的 noteref/note 需：术语处加 `<a epub:type="noteref">`、对应 `<aside epub:type="footnote">` 或 note 文档，阅读器负责弹窗。实现与测试成本高，且阅读器支持不一，建议需求强烈再做。 |

**补充建议**：
- 公式与代码高亮可做成「可选增强」：仅当检测到公式/代码块时启用，避免所有书都走重逻辑。
- 交叉引用可先支持「同文档内锚点」，再考虑「跨文档」。

### 3.1 学术增强 — 细规格（仅规划）

**3.1.1 数学公式 LaTeX → SVG**

| 项 | 规格 |
|----|------|
| 输入 | 正文中的 LaTeX 片段（如 `$...$`、`\[...\]`、或已有 `<math>` 的 content 需从 LaTeX 转）。 |
| 输出 | 内联或块级 SVG；可选保留/生成 MathML 或 aria-label 以做可访问性。 |
| 触发 | 仅当检测到公式语法或配置开启时执行；避免全书走公式渲染。 |
| 依赖选型 | **latex2svg**（Python，单公式）、**MathJax-node**（服务端）、**Pandoc**（math 转 MathML 再转 SVG 或直接输出 SVG）。严禁低清位图。 |
| 衔接 | 在 HTML 清洗链中增加「公式渲染」步骤（或接在 MD→HTML 之后）；输出替换原占位为 `<span class="math"><svg>...</svg></span>` 等，StemGuard 已保护 SVG 不被后续清洗破坏。 |

**3.1.2 代码高亮（Markdown 代码块 → HTML + CSS）**

| 项 | 规格 |
|----|------|
| 输入 | 已解析出的代码块（如 `<pre><code class="language-xxx">` 或原始 ```xxx 块）。 |
| 输出 | 带语义化 class 的 HTML（如 `<span class="keyword">`）+ 一份注入的 CSS 高亮主题。 |
| 触发 | 仅当存在代码块时启用；可选「主题」参数（如 github / monokai）。 |
| 依赖选型 | **Pygments**（Python，多语言）；或 Pandoc 的 highlight-style + 自备 CSS。 |
| 衔接 | 若 MD 入口用 Pandoc，可 Pandoc 直接输出高亮 HTML；否则在 HTML 清洗链中增加代码高亮 Cleaner（仅处理 pre/code）。 |

**3.1.3 正文内交叉引用 (Cross-reference)**

| 项 | 规格 |
|----|------|
| 输入 | 正文中的引用文本（如「见第 3 章」「图 1-2」「Section 2.1」）。 |
| 输出 | 替换为 `<a href="chapter.xhtml#anchor">...</a>`，且目标章节确有对应 anchor（与 TocRebuilder 的 anchor 或图/表 id 一致）。 |
| 范围 | 先做「同文档内锚点」；跨文档需解析 spine 顺序与 TocRebuilder 生成的 id。 |
| 衔接 | 独立 Cleaner 或与 TocRebuilder 同层后处理；可依赖 TocRebuilder 已写入的 anchor 表。 |

---

## 4. 多语言与翻译流 (Translation Workflow)

| 功能项 | 现状 | 难度 | 建议 | 备注 |
|--------|------|------|------|------|
| **双语对照（段落级/左右栏）** | **已有** | — | — | **已有**：SemanticsTranslator 的 bilingual 模式，段落级原文+译文，class `epub-original` / `epub-translated`。左右栏需额外 CSS 布局，当前为上下段落对照。 |
| **翻译后处理（修标签/公式/图文）** | 未实现 | 高 | ✅ | 自动检测并修复 MT 导致的标签缺失、公式截断、图文分离。可做成独立「翻译后处理」Cleaner，依赖规则或小模型检测异常。 |
| **术语表 (Glossary)** | **已有**（输入侧） | — | — | **已有**：job.glossary 传入翻译 prompt，强制术语一致。 |
| **术语表自动提取 + 独立术语页** | 未实现 | 高 | ⚠️ | 从译文自动抽词并生成「术语表」XHTML 页。涉及抽取逻辑、排序、与 Nav 的集成。若不做自动提取，可保留「用户上传术语表」即可。 |
| **语义锚点自动化** | 未实现 | 中 | ✅ | 开发函数：为双语排版中的中英文段落自动生成**对称 ID**（如 `p-en-001` 与 `p-zh-001`），用于左右栏或上下对照时的强制对齐与锚点跳转。 |

**补充建议**：
- 翻译后处理优先做「标签闭合检测」和「公式完整性检查」，再考虑图文分离等。
- 左右栏双语若做，需在 CSS 中做响应式（大屏双栏、小屏上下），并标注阅读器兼容性。

### 4.1 翻译后处理与左右栏 — 细规格（仅规划）

**4.1.1 翻译后处理（修标签/公式/图文）**

| 项 | 规格 |
|----|------|
| 输入 | 翻译后的章节 XHTML（已由 SemanticsTranslator 或章节级任务产出）。 |
| 检测 | 标签闭合：栈检查，发现未闭合标签则记录位置或尝试自动闭合；公式完整性：检测 `<math>`/`<svg>` 是否被截断（如缺闭合标签）；图文分离：检测 `<img>`/`<svg>` 与相邻文本是否错位（启发式）。 |
| 修复 | 自动闭合可确定的标签；对无法自动修复的段落打标或跳过并记录日志，便于人工抽检。 |
| 衔接 | 放在「翻译结果写回章节」之后、全书 Reduce 之前，或作为 Reduce 前的一道 Cleaner；仅对「已翻译」的 body 章节执行。 |

**4.1.2 左右栏双语布局**

| 项 | 规格 |
|----|------|
| 输入 | 已有段落级双语（`epub-original` / `epub-translated`）。 |
| 输出 | 同一段落的原文与译文在阅读器内呈左右两栏（大屏）或上下块（小屏），通过 CSS 控制。 |
| 实现 | 新增「双语布局」预设：注入 CSS（如 grid 或 float），使 `.epub-original` 与 `.epub-translated` 同段并排；媒体查询在小屏下改为上下堆叠。 |
| 衔接 | 不改变现有翻译输出结构，仅通过 CSS 与可选的外层包裹 class 控制展示；与现有 bilingual 开关兼容。 |

**4.1.3 语义锚点自动化（中英段落对称 ID）**

| 项 | 规格 |
|----|------|
| 目标 | 为双语排版中的原文段落与译文段落生成一一对应的 ID，便于强制对齐与锚点跳转（如「点击原文某段滚动到对应译文」）。 |
| 命名规则 | 示例：原文段落 `id="p-en-001"`，对应译文段落 `id="p-zh-001"`；或按章节前缀 `c01-p-en-001` / `c01-p-zh-001`。规则可配置（语言码、分隔符、序号位数）。 |
| 输入 | 已包含 `epub-original` / `epub-translated` 的章节 HTML（或 chunk 合并后的章节）。 |
| 输出 | 在对应段落节点上写入 `id` 属性；保证同一「段落对」序号一致。若段落已存在 id，可选择保留或覆盖（建议覆盖为统一规则）。 |
| 实现 | 开发独立函数：遍历 DOM，识别成对的原文/译文块，按顺序赋 `p-{lang}-{seq}`；可在 chapter_reduce 或翻译后处理阶段调用。 |
| 衔接 | 与 SemanticsTranslator 的 bilingual 输出、左右栏 CSS 配合；TocRebuilder 不依赖此 ID，但若需「双语 TOC」可复用同一序号体系。 |

---

## 5. 多媒体与图形 (Multimedia)

| 功能项 | 现状 | 难度 | 建议 | 备注 |
|--------|------|------|------|------|
| **SVG 矢量集成与保护** | **部分已有** | — | — | **部分已有**：Packager 内 SVG 大小写属性修复；StemGuard 保护 MathML/SVG 不被清洗器破坏。无「艺术字/书法」专用 SVG 流水线。 |
| **HTML5 \<audio\> 支持** | 未实现 | 中 | ⚠️ | 需：允许 `<audio src="...">`、将音频文件打入 EPUB、manifest 声明。若目标主要是文字书，可延后；有声书/教材再上。 |
| **资源自动化处理（图片）** | 未实现 | 中 | ✅ | 自动检测 EPUB 内图片分辨率，若超过阅读器常见限制则自动压缩；若涉及书法、艺术字等**高频细节**图片，自动转为高度优化的 WebP 或 SVG，在保证清晰度的前提下控制体积与兼容性。 |

**补充建议**：
- 若强调「艺术字体/书法」：可增加「 SVG 优化」步骤（压缩、安全过滤），避免嵌入不可信脚本。

### 5.1 多媒体 — 细规格（仅规划）

| 项 | 规格 |
|----|------|
| **SVG 优化（可选）** | 输入：EPUB 内 SVG。行为：压缩（去除冗余元数据）、安全过滤（移除 script/onload 等）。与现有 Packager 的 SVG 大小写修复、StemGuard 兼容。 |
| **HTML5 \<audio\>** | 输入：HTML 中含 `<audio src="...">`。行为：将音频文件打入 EPUB、在 manifest 中声明、不修改标签。仅当产品明确支持「有声书/朗读」时实现。 |
| **图片分辨率检测与压缩** | 输入：EPUB 内嵌图片（如 PNG/JPEG）。行为：解析尺寸与 DPI，若超过设定阈值（如 2048px 宽或常见阅读器限制）则缩放或重编码压缩；输出仍为 PNG/JPEG 或可选 WebP。衔接：在 Unpack 后、打包前增加「图片处理」步骤；不改变 HTML 引用路径，仅替换资源文件。 |
| **高频细节图 → WebP/SVG** | 输入：上述图片中可识别为「书法/艺术字/线稿」等高频细节类型（可启发式：高分辨率、低色数、或用户标记）。行为：优先转为高度优化的 WebP（有损/无损可选）或矢量 SVG（若为简单图形）；控制体积并保持可读性。与「图片分辨率检测」同流水线；可选开关 `optimize_art_images: bool`。 |

---

## 6. 与现有管线衔接、验收与依赖选型（总览）

### 6.1 与现有管线衔接点

| 新能力 | 建议接入位置 | 说明 |
|--------|--------------|------|
| MD/DOCX 入口 | 在 converter 层增加「输入类型分支」：若为 .md/.docx，先转成 (book 或 items)，再走与 EPUB 相同的 Unpack 后路径（或直接构造 book 进 Cleaner 链）。 | 与现有 `convert_file_to_horizontal` 的 epub/pdf 分支并列。 |
| 行距/首字/分页/字体 | Cleaner 链中新增或并入 TypographyEnhancer；字体嵌入在打包前写入 assets 并改 manifest。 | 顺序：CjkNormalizer → … → TypographyEnhancer（或新 Cleaner）→ TocRebuilder → Packager。 |
| 公式/代码高亮 | 若从 MD 进：可在 Pandoc 输出阶段完成；若从 EPUB 进：在 HTML Cleaner 链中增加一步，且位于 StemGuard 之前或与 StemGuard 协同。 | 保证 StemGuard 不破坏已生成的公式/代码 SVG 或 HTML。 |
| 翻译后处理 | 在章节 Reduce 回写之后、或作为 Reduce 前对「单章 HTML」的一道处理；仅对 enable_translation 且为 body 的章节执行。 | 与 chapter_translation_service、chapter_reduce_service 配合。 |
| 代码瘦身增强 | 扩展 CssSanitizer 或独立 Cleaner，在现有「移除内联样式」之后增加 span 合并与 class 清理。 | 仅处理 HTML(9)。 |
| 多端适配层 | 在注入 CSS 的环节（TypographyEnhancer 或独立 Cleaner）增加 prefers-color-scheme 与 Kindle 专用 Hack；可维护 kindle.css 或条件样式。 | 与 DeviceProfile / 全局样式注入同层。 |
| 语义锚点自动化 | 在 chapter_reduce 或翻译后处理中调用「对称 ID 生成」函数，为 epub-original / epub-translated 段落对写入 p-en-xxx / p-zh-xxx。 | 依赖 bilingual 输出结构。 |
| 资源自动化处理 | Unpack 后、打包前增加「图片处理」流水线：检测分辨率 → 超限则压缩；可选对书法/艺术图转 WebP 或 SVG。 | 仅处理 item 类型为 image 的资源，不改 spine。 |
| 竖排标点修正 | 竖排分支中增加一步：正则或 DOM 替换弯引号→直角引号（及可选旋转）；可做成独立 Python 脚本与格式转换同流程执行。 | 仅在「竖排输出」路径启用，与 CjkNormalizer 横排路径解耦。 |

### 6.2 验收与测试要点

| 类别 | 要点 |
|------|------|
| 排版 | 每项新排版能力（行距、首字、分页、字体）至少 1 个回归用例；输出 EPUB 必须通过 epubcheck。 |
| 多源输入 | MD/DOCX 各至少 1 个 E2E：输入文件 → 输出 EPUB → epubcheck 通过；与现有 EPUB 路径共享同一套 Cleaner。 |
| 学术 | 公式：至少 1 个含 LaTeX 的样本转成 SVG 且无截断；代码高亮：至少 1 个含代码块的 MD 转 EPUB 且高亮正确。 |
| 翻译后处理 | 至少 1 个「含故意损坏标签」的翻译结果经后处理后闭合或打标。 |
| 多端适配 | 深色/浅色系统下 EPUB 可读；Kindle 或模拟器上无严重错位。 |
| 语义锚点 | 双语 EPUB 中原文/译文段落具备对称 ID，且序号连续无冲突。 |
| 资源处理 | 超限图片被压缩；书法/艺术图（或测试样本）可转为 WebP/SVG 且体积与显示符合预期。 |
| 竖排标点 | 竖排输出中弯引号已替换为直角引号，无残留横排引号。 |

### 6.3 依赖与选型建议（汇总）

| 能力 | 推荐选型 | 备选 |
|------|----------|------|
| MD → HTML | Pandoc | Python markdown + extensions |
| DOCX → HTML | Pandoc | python-docx + 自建 XHTML |
| LaTeX → SVG | latex2svg / MathJax-node | Pandoc math → MathML 再转 SVG |
| 代码高亮 | Pygments 或 Pandoc highlight | highlight.js 服务端 |
| 字体子集化（可选） | fonttools (pyftsubset) | — |
| 图片压缩/缩放 | Pillow (PIL) | — |
| 图片转 WebP | Pillow 或 cwebp | — |

---

## 7. 与「Word/Markdown → EPUB」强相关的汇总（优先级）

| 能力 | 状态 | 建议优先级 |
|------|------|------------|
| .md → XHTML 入口 | 未实现 | P0（与口号直接相关） |
| .docx → XHTML 入口 | 未实现 | P1（可放 .md 之后） |
| 代码瘦身 + 清洗 | 部分已有 | P0（增强现有 CssSanitizer） |
| 分页、首字下沉、行/段间距 | 未实现 / 部分 | P1 |
| 字体嵌入 | 未实现 | P1 |
| 公式 LaTeX→SVG | 未实现 | P1（学术场景） |
| 代码高亮 | 未实现 | P2（随 MD 入口一起更划算） |
| 多端适配层（prefers-color-scheme + Kindle） | 未实现 | P1 |
| 语义锚点自动化（p-en-xxx / p-zh-xxx） | 未实现 | P1（双语场景） |
| 资源自动化处理（图片压缩 / WebP·SVG） | 未实现 | P1 |
| 繁体竖排标点修正（弯引号→直角引号） | 未实现 | P1（若做竖排输出则必做） |

---

## 8. 不建议做或谨慎做的项

| 项 | 建议 | 原因 |
|----|------|------|
| **横排→竖排 (LTR→RTL)** | ⚠️ 谨慎 | 与现有「竖转横」主流程相反，实现与测试成本高；除非有明确竖排出版需求。 |
| **epub:type noteref 弹出注释** | ⚠️ 延后 | 阅读器支持不一，调试成本高；可先用「脚注同页展开」等简单形式。 |
| **术语表自动提取 + 独立页** | ⚠️ 延后 | 抽取与产品形态复杂；当前「用户提供术语表」已覆盖主要场景。 |
| **HTML5 \<audio\>** | ⚠️ 按需 | 非文字书核心路径；有明确有声/教材需求再上。 |
| **在现有 pipeline 中混入「竖排输出」** | ❌ 不建议 | 易与现有 CjkNormalizer、设备配置纠缠；若做竖排，建议单独模式或分支。 |

---

## 9. 文档与产品补充建议

- **产品**：在官网/介绍中明确「一站式排版服务」包含：**输入**（EPUB/PDF，规划中 Word/MD）、**排版**（繁简、横竖、分页、字体、首字下沉）、**翻译与双语**、**学术增强**（公式、代码、目录与交叉引用）。
- **技术**：为 Word/MD 规划一条「预处理 → 现有 Cleaner 管线 → 打包」的清晰边界，避免与现有 EPUB/PDF 路径强耦合。
- **测试**：新增能力建议同步增加回归用例（如 run_regression 或单独 typography 套件），防止排版/清洗回退。

---

*文档版本：规划版 v3，补充多端适配层（prefers-color-scheme + Kindle Hack）、语义锚点自动化（p-en-xxx / p-zh-001）、资源自动化处理（图片分辨率检测与压缩、书法/艺术图→WebP·SVG）、繁体竖排标点修正（弯引号→直角引号、正则替换与旋转）。仍不涉及具体实现代码。*
