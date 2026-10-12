# PDF 排版与忠实度优化复验

> **历史归档（2026-10-12 整理）**：下文保留记录日期当时的实现、配置、测试和发布状态；“当前”“已上线”“未提交”等均指当时，不代表今天的状态。本次仅整理留存资料，未重新执行历史测试、生产操作或外部调用。历史命令不是当前操作指引；阅读入口见[历史记录索引](HISTORICAL-RECORDS.md)，最近已验证的生产发布见[2026-10-12 发布记录](PRODUCTION-RELEASE-2026-10-12-MANUAL-REVIEW.md)。
>
> **版本边界**：本文所述扫描 OCR、PDF 翻译、旧报价及旧开关是历史方案。当前产品采用独立的文字型 PDF → 原文 EPUB，图片保持原样；不要照本文恢复旧公共接口或开关。参见[README](../README.md)和[PDF 范围说明](PDF-TRANSLATION-ARCHITECTURE.md)。

日期：2026-09-27。范围：中文原文提取、已有 OCR 缓存后处理、EPUB 构建。真实书籍本轮没有新增 OCR 或翻译调用，没有部署或推送。

前置证据见 [六页 OCR 验证记录](PDF-GALOIS-VALIDATION-2026-09-27.md)。本轮不能代表整书内容已经验收。

## 已实现

1. **公式本地排版**：支持 `$…$`、`$$…$$`、`\(…\)`、`\[…\]` 和独立公式块。使用固定版本的 latex2mathml、ziamath 与随包数学字体生成无脚本、无外部资源的 SVG。字体缺少中文字形时保留 MathML 和原页链接；未知命令、过长输入及用空格／下划线模拟的多行推导保留原文与原页链接，不猜测数学结构。修复渲染库对 `\mathrm{i}` 的直立体处理问题。
2. **中文段落**：字号计入 PDF 文本矩阵缩放；按观测行距、满行程度与首行缩进重组。下一行缩进、短行、代码与标题等继续保留边界。OCR 重复的 `paragraph_id` 不再自动消除中文句末、对话或空行边界。旧缓存保留原始文本，离线重新分段；无法恢复的原始块边界不作推断。
3. **文字差异记录**：增加有顺序的字符比对，检测增字、漏字和替换。只忽略空白，不归一化数学符号。私有检查点保存偏移与有限上下文，不自动用文字层覆盖 OCR。长页跳过昂贵的精确对齐并标记原因。
4. **文字层分流**：识别着重号、圈号、MSAM/MSBM 数学字体等潜在映射错误；少量私用字符也会触发 OCR 分流。没有全局删除数字或替换字母。
5. **缓存和语言**：为后处理和 EPUB 产物增加版本；旧 OCR 缓存可重建而不重新计费。新成功响应也保留原始 JSON。中文样本使用 `zh-Hans`，构建器可显式指定语言。
6. **下游保护**：MathML 按不可翻译内容保护，避免进入正文翻译、文本修复和覆盖率统计。原有的公式代码保护仍保留。

修改位置：

- `backend/app/domain/pdf/math_render.py`
- `backend/app/domain/pdf/{native_layout,local,text_quality,content,providers,pipeline}.py`
- `backend/app/engine/chunk_extractor.py`
- `backend/app/engine/cleaners/semantics_translator.py`
- `backend/requirements.txt`、`backend/requirements.lock`
- `backend/test_d44_pdf_readability.py`：15 项新增回归。

原有未提交的 PDF 实现、交付可靠性修改等均保留，本次没有将全部工作区变化归为自己的修改。

## 同一本书离线复验

原书 SHA-256：`9ef7cc2be4c8d1825b6b8a67e57d5ef1c3e2e789d43ddae98e0be0df2c3b5dcc`。

| 检查 | 结果 |
| --- | --- |
| 新增真实模型调用 | 0；只使用先前获准的六页缓存 |
| 公式显示 | 56 处 SVG（25 个不同资源）、1 处 MathML；2 组复杂推导保留原文及原页链接 |
| 第 320 页 | 正文段落从 6 个恢复为 13 个，另保留 4 个标题 |
| 第 399 页 | 正文段落从 8 个恢复为 12 个，另保留 2 个标题 |
| 第 122 页 | 差异记录检测到新增的“便”；仍为 `missing_confidence_field` 待核验，没有补填字段或放行正文 |
| 7 个已知文字层问题页 | 第 34、45、122、292、320、399、427 页均不再直接采用原文字层 |
| 新预检分流 | 54 页 local、423 页 OCR；原先为 72／405。保守拦截多了 18 页，可能增加完整处理成本；未调用这些页面的 OCR |
| 54 个仍走 local 的页面 | 文字块 1177 → 974；检查源字符保持一致。块数变化只是排版证据，不是准确率 |
| 原页图片 | 六张图片逐字节保留 |
| EPUBCheck 5.1.0 | 通过，0 警告 |
| 视觉检查 | 离线渲染并查看 25 个不同 SVG；修复并复查直立体 i。浏览器本机预览返回 `ERR_BLOCKED_BY_CLIENT`，未完成实际阅读器的 MathML 显示验证 |

样本 SHA-256：`780aeaeb149189078eb08ae88fbc5d86f525f4f3015d7ae9ff4065905b964a7f`。

## 测试

使用临时 Python 3.12 环境与官方 EPUBCheck 5.1.0、临时 Java 17 运行环境。渲染依赖固定为 `ziamath==0.13`、`ziafont==0.11`、`latex2mathml==3.81.1`；没有在生产机安装。本机临时测试环境中的 PyMuPDF 仅用于视觉检查，不属于应用新增依赖。

```sh
PYTHONPATH=backend PYTHON_DOTENV_DISABLED=1 python -m unittest \
  test_d37_pdf_pipeline test_d37_pdf_providers \
  test_d38_pdf_acceptance_fixes test_d39_pdf_concurrency_reflow \
  test_d40_pdf_translation_acceptance test_d41_pdf_fault_soak \
  test_d42_pdf_native_layout test_d44_pdf_readability \
  test_d12_translation_enhancement test_d16_translation_qa_service \
  test_d18_paid_translation_regression -q
```

需要将 Java 加入 `PATH` 并设置 `EPUBCHECK_JAR`。结果：103 项通过，包括最终 EPUB 格式校验、强杀恢复、缓存复用不重复请求与 1000 次故障模拟。另运行包含中文 MathML 的完整模拟翻译链路，通过且 MathML 内容保留。所有翻译／OCR 响应均为测试模拟，没有翻译这本书。最后的小范围公式调整后，15 项新增回归再次通过。`git diff --check` 通过。

## 私有产物

含书籍内容的文件均在被 Git 忽略的 `backend/temp/galois-pdf-validation-20260927/optimized/`：

- `galois-optimized-sample.epub`：六页原文排版样本，仍含第 122 页待核验提示。
- `audit.json`、`epubcheck.json`：统计、原图检查、产物哈希和格式结果。
- `inspection.json`：新版全书本地预检。
- `comparison.json`：文字层与 OCR 的差异明细；第 122 页只是待核验候选比对，没有转为可交付正文。
- `page-*.json`：新版页级结果与原始文本记录。
- `preview/`：从最终 EPUB 提取的静态文件。
- `formula-preview-*.png`、`upright-i-check.png`：SVG 视觉检查记录；第一组拼图中的直立体 i 是修复前证据，以单独复查图和最终 EPUB 为准。

## 尚未完成的验收范围

- 第 122 页 OCR 的增字和缺字段仍需按原图处理；两组多行推导没有擅自重构。
- 页眉、页边文字和着重号样式仍未全面恢复；中文段落采用保守规则，可能多分段。
- MathML 在不同电子书阅读器上的显示兼容性尚未实机验证。
- 本轮改进适用于新预检；已确认任务保留原预检分流，不会因新规则偷偷扩增 OCR 请求。
- 新提示词的字段完整性与段落识别效果尚未用新增真实请求验证。
- 面向用户的“纯 PDF 转原文 EPUB”产品入口、计价与整书交付流程尚未实现，不能把这份六页样本称为完整成品。
