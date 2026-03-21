# PDF 接入设计草图 v1.1（仅规划，不实现）

目标：为现有 `epub-factory` 增加 `pdf -> epub` 能力，并为后续 `pdf <-> word` 打基础，保持系统可回滚、可观测、低风险上线。

---

## 1. 工程边界与原则

- 仅新增“PDF 解析入口”，不改动现有 EPUB 核心清洗/打包主流程。
- 采用插件化：`pdf_adapter` 独立模块，失败可降级回旧流程（不影响已有 epub 功能）。
- 先做“文本型 PDF”MVP，再扩展 OCR/复杂表格。
- 可靠性优先：解析失败时明确错误码 + 可追踪日志，不静默吞错。

---

## 2. 目标能力范围

### Phase 1（MVP）
- 输入：PDF（文本型）
- 输出：EPUB（可读优先，不追求版式完全还原）
- 支持：
  - 标题/段落提取
  - 图片基础提取
  - 简单目录重建
  - 进入现有繁简转换与排版优化链路

### Phase 2（增强）
- OCR 扫描件（hybrid）
- 复杂表格提取增强
- 多栏阅读顺序增强

### 暂不承诺
- 完整保真排版复刻
- PDF 公式 100% 还原

---

## 3. 建议技术选型

主解析引擎：`opendataloader-pdf`
- 理由：支持 `markdown/json/html` 多输出，适合作为中间层。
- 适配形态：作为 `pdf_adapter` 调用，不直接侵入现有核心业务模块。
- 部署约束：需要 Java 11+（需在本地/服务器镜像中显式声明）。

参考项目：
- [opendataloader-project/opendataloader-pdf](https://github.com/opendataloader-project/opendataloader-pdf)

---

## 4. 架构设计（新增模块）

建议新增目录：

- `backend/app/adapters/pdf_adapter.py`
- `backend/app/domain/pdf_to_epub_service.py`
- `backend/app/models/convert_models.py`（统一请求/响应结构）

职责拆分：

- `pdf_adapter`
  - 负责调用 opendataloader
  - 输出标准中间结构 `DocumentIR`
- `pdf_to_epub_service`
  - 将 `DocumentIR` 转为现有 EPUB 引擎可消费格式
  - 复用现有清洗、打包、任务状态管理

---

## 5. 统一中间结构（DocumentIR）

建议定义（逻辑层）：

- `meta`: title, author, language
- `meta`: title, author, language, source_file_hash, page_count
- `blocks[]`:
  - `type`: heading | paragraph | list | table | image | caption
  - `block_id`: 全局唯一 ID（便于 trace 与回查）
  - `reading_order`: 阅读顺序序号（多栏场景关键）
  - `section_id`: 所属章节 ID（便于 TOC 重建）
  - `level`: 标题层级（可选）
  - `content`: 文本内容（可选）
  - `confidence`: 结构置信度 0-1（可选）
  - `bbox`: 坐标（可选）
  - `page`: 页码（可选）
  - `assets_ref`: 图片资源引用（可选，需明确生命周期：提取至临时目录 -> 打包入 EPUB -> 随任务清理，防止磁盘撑爆）
  - `table_schema`: 表格结构摘要（列数、表头、合并单元格信息，可选）

价值：
- 后续接 `word/md/azw3` 时复用同一中间层，减少重复开发。

---

## 6. API 规划（草案）

新增接口建议：

- `POST /api/v2/convert/pdf-to-epub`
  - 入参：文件 + 模式参数
  - 参数：
    - `mode`: `fast|accurate`
    - `ocr`: `auto|off|force`
    - `idempotency_key`: 幂等键（可选，避免重复扣费/重复任务）
  - 出参：任务 ID + 状态查询地址

接口约束建议（MVP）：
- 文件大小上限：`<= 50 MB`
- 页数上限：`<= 500`
- 单任务超时：`15 min`
- 并发配额：同租户 `2` 个并发任务（其余排队）
- 超限返回：`413/422`（文件或页数限制）

复用现有任务中心：
- `GET /api/v2/jobs/{job_id}` 查看进度与失败原因。
- **长时任务推送（建议演进）**：对于百页以上大体积 PDF 解析，建议后续引入 Server-Sent Events (SSE) 或 WebSocket 替代单纯的长轮询，以优化前端体验并降低服务器连接占用。

---

## 7. 可观测性与回滚

每次转换记录：
- `trace_id`
- `engine`: opendataloader/local/hybrid
- `latency_ms`
- `pages_total`
- `blocks_extracted`
- `fallback_used`（是否降级）
- `error_code`（若失败）
- `cost_estimate`（预估成本）
- `cost_actual`（实际成本，若可得）
- `retry_count`
- `queue_wait_ms`
- `timeout_hit`（是否触发超时）

回滚策略：
- Feature Flag：`ENABLE_PDF_TO_EPUB`
- 关闭后直接禁用新入口，不影响现有 epub 功能。

成本控制策略（新增）：
- 默认走 `local` 引擎；仅当结构质量低于阈值时才尝试 `hybrid`。
- 单任务成本上限（示例）：`$0.30`；超过阈值直接降级到 `local` 并返回 `COST_GUARDRAIL_TRIGGERED`。
- 对 OCR 任务设置独立阈值与开关：`ENABLE_PDF_OCR`。
- 成本阈值可配置并按环境区分（dev/staging/prod）。

---

## 8. 风险清单与应对

- Java 依赖缺失
  - 应对：启动自检 + 健康检查暴露依赖状态。
- JVM 频繁冷启动导致 CPU 飙升与 OOM（核心性能风险）
  - 问题：`opendataloader-pdf` 默认每次转换会 spawn 一个新的 JVM 进程，并发场景下开销极大。
  - 应对：实施阶段严禁在 Web 线程直接通过 `subprocess` 调用。建议将其作为常驻后台服务（Daemon）运行并暴露内部接口，或通过 Celery 队列严格限制解析 Worker 的并发数。
- 临时图片堆积导致磁盘打满
  - 应对：为 `assets_ref` 制定严格的生命周期管理，转换成功或失败后，必须在 `finally` 块或清理脚本中强制删除从 PDF 中解包的临时图片素材。
- 大文件慢/超时
  - 应对：异步任务 + 页数限制 + 超时断路。
- 扫描件失败率高
  - 应对：MVP 默认只支持文本型；OCR 放到 Phase 2。
- 结果质量波动
  - 应对：质量评分 + 人工抽检样本集 + 回归测试。
- 第三方库升级导致行为漂移
  - 应对：固定版本 + 金丝雀发布 + 回归集门禁。

失败恢复策略（新增）：
- 重试策略：仅对可重试错误（网络抖动/临时 IO）重试，最多 `2` 次，指数退避。
- 不可重试错误（格式损坏/密码保护）立即失败并返回可读错误码。
- 失败保留物：保留 `DocumentIR` 草稿与错误上下文（脱敏），用于排障。
- 人工介入点：任务状态 `NEEDS_REVIEW`，支持后台重跑并切换模式。

---

## 9. 验收标准（MVP）

- 30 份文本型 PDF 测试集中：
  - 任务成功率 >= 90%
  - 可读性达标（章节和段落顺序正确）>= 85%
  - 平均耗时可控（按你当前实例规格设基线）
- 失败任务必须返回可理解错误信息（非空）。

可量化评分口径（新增）：
- 阅读顺序准确率：人工标注顺序对比，`>= 0.85`
- 标题层级准确率：`>= 0.80`
- 表格可用率（文本型简单表格）：`>= 0.75`
- 可读性评分计算：`0.5*阅读顺序 + 0.3*标题层级 + 0.2*段落完整性`
- 回归门禁：任一核心指标回退 `>5%` 阻断发布

样本集建议：
- 文本型单栏 10 份、双栏 10 份、含图表 10 份（覆盖中英混排）

---

## 10. pdf <-> word 规划建议（并行准备）

优先顺序：
1. `word -> pdf`（稳定，先上线）
2. `pdf -> word`（Beta，质量波动更大）

建议工具：
- `word -> pdf`: LibreOffice headless
- `pdf -> word`: 先用转换引擎 + 二次清洗（标题/段落修复）

说明：
- `opendataloader-pdf` 不是 Word 引擎，主要负责 PDF 结构提取。

---

## 11. 里程碑（建议）

- M1（1 周）：模块骨架 + DocumentIR + 文本型 PDF MVP 设计评审
- M2（1-2 周）：接入任务流、日志与错误码、灰度开关
- M3（1 周）：样本回归、性能压测、上线预案

依赖治理与上线门禁（新增）：
- 锁定 `opendataloader-pdf` 与 Java 运行时版本（避免非预期升级）
- 启动时依赖检查：
  - `java -version` 必须通过
  - `opendataloader` 可执行探针通过
- CI 门禁：
  - 样本回归测试通过
  - 指标未退化
  - Feature Flag 默认关闭，仅灰度环境开启

> 当前文档为“规划版”。待你确认后，再进入实现阶段。

