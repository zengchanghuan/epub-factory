# EPUB Factory 格式转换服务路线图

本文档规划了后续扩展的格式转换功能。目前仅作规划，暂不实现。

---

## 1. AZW3 转 EPUB (AZW3 to EPUB)
**目标**：让 Kindle 专有格式可以在其它设备（如 Apple Books）阅读。

### 技术路线
- **方案 A (推荐)**: 使用 Python 库 `kindleunpack` (MobiUnpack) 直接提取 HTML。
  - 优点：纯 Python，轻量，适合服务器运行。
  - 缺点：对某些复杂的 KF8 特性支持可能不完美。
- **方案 B**: 使用 Calibre 的 `ebook-convert`。
  - 优点：格式转换最强，支持最全。
  - 缺点：安装包极大（几百 MB），依赖多，不适合轻量级 Docker/Lambda。

### 开发重点
- 处理 DRM（仅支持无加密文件）。
- 保持图片和内嵌字体的完整性。

---

## 2. Word 与 EPUB 互转 (Word ↔ EPUB)
**目标**：方便创作者将手稿一键转为电子书，或将电子书转回 Word 修改。

### 技术路线
- **Word → EPUB**:
  - 使用 `python-docx` 或 `mammoth` 提取结构化内容。
  - 重点在于提取“标题样式”并将其映射为 EPUB 的章节。
- **EPUB → Word**:
  - 使用 `pandoc` 或手动解析 HTML 并用 `python-docx` 生成。
  
### 开发重点
- **样式清洗**：Word 的 HTML 极其凌乱，必须经过严格的 CSS 清洗。
- **目录生成**：从 Word 标题自动生成 NCX/OPF 目录。

---

## 3. Markdown 与 EPUB 互转 (MD ↔ EPUB)
**目标**：为程序员和写作爱好者提供极简的电子书制作工具。

### 技术路线
- **方案**: 深度集成 `Pandoc`。
  - Pandoc 是文档转换界的“瑞士军刀”，对 MD 转 EPUB 支持极佳。
  - 可以自定义 YAML Frontmatter（标题、作者、封面）。
  
### 开发重点
- **数学公式**：支持 MathJax 或 MathML。
- **代码高亮**：在 EPUB 中内嵌 Prism.js 或类似的高亮 CSS。
- **批量处理**：支持将多个 MD 文件合并为一个带有 TOC 的 EPUB。

---

## 4. 架构调整
为了支持多格式转换，需要对现有的 `converter.py` 进行重构：
- **插件化架构**：每个转换器（azw3_to_epub, docx_to_epub）作为一个独立的模块。
- **统一中间件**：所有格式先转为统一的“中间 HTML + 图片”结构，再统一打包为 EPUB，从而复用现有的“繁简转换”和“排版优化”逻辑。
