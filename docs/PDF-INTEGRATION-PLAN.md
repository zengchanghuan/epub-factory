# PDF 接入方案入口

当前方案见 [PDF 翻译技术架构](PDF-TRANSLATION-ARCHITECTURE.md)。

2026-09-21：已按用户要求移除旧解析引擎的选型、依赖、配置及部署建议。此文件仅保留入口，避免其他文档的链接失效。

当前设计：文本、扫描与混合 PDF；MinerU 免费云端 API 与 DeepSeek Flash 双通道；超出 MinerU 单文件限制或高优先级额度直接走 Flash；输出 EPUB 与原 PDF 页码对照。

状态：待审核，未实现 PDF/OCR 功能。
