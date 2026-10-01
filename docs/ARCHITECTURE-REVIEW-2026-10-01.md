---
title: EPUB Factory 架构审查
date: 2026-10-01
code_revision: 5d1705c9fed54b93403d34f2a4b70f4bcfc2e3b8
status: reviewed-not-fixed
scope: local-code-and-offline-verification
---

# EPUB Factory 架构审查

## 1. 结论与核验范围

当前架构适合继续作为**模块化单体 + 异步整书 Worker**演进，没有证据表明此时拆成微服务会改善最主要的问题。优先事项是修复交易权益、可靠调度与成品一致性，再统一转换和翻译执行计划。

依据为本地提交 `5d1705c` 的代码、真实调用关系与隔离离线测试；审查时没有访问生产、真实支付、调用付费模型或重译用户书稿。下文保留审查时尚未修复的缺陷快照，不等于已经确认历史订单受到影响。后续授权实施与逐项门禁状态见 [优化与历史书稿回归](ARCHITECTURE-OPTIMIZATION-2026-10-01.md)，不能把本报告当作修复完成清单。

当前组件图、执行链与存储边界见 [当前架构](ARCHITECTURE-DIAGRAM.md)。P1 表示可能造成未授权消耗、收费不履约、错误成品或任务永久失联，应优先修复；P2 表示特定并发、入口或扩容条件下的可靠性/维护风险。

### 1.1 值得保留的设计

- 输入校验、EPUB 结构校验、chunk QA、最终成品审计分层，交付判断不只依赖模型返回成功。
- 翻译有 attempt 身份、执行租约、取消检查和独立临时成品；这些机制有价值，但数据库 fencing 和异常恢复仍有缺口。
- 有界补译、批次拆分、文本节点救援、配置隔离缓存、准备阶段与 chunk 检查点，避免每次全书重来。
- 画像、文体、术语、人物与只读上下文已进入 EPUB 翻译主链，不依赖并发完成顺序更新可变窗口。
- 模型费用账本与报价分离，记录逐请求用量与计价版本。离线验证确认：模型返回后账本落盘失败会传播 `AccountingError`，没有被普通重试吞掉并再次调用模型。
- 支付已有验签、订单身份与冻结金额校验；邮件已有持久化 outbox；部署已有锁、活跃任务检查、备份与代码哈希检查。这些不是本轮发现的缺陷。

## 2. P1：优先修复的具体问题

### R1. 未付费任务可以经取消后重启绕过付款，重译也能越过原购买档位

**触发与影响：** 用户持有自己任务的访问令牌即可取消待画像确认任务。取消后重启只检查执行状态、源文件和重译次数，没有验证已付权益；请求还能更改质量档位与模型。未付款任务会进入执行队列，已经付款的普通档任务也没有升级补差价边界。

**证据：** [取消入口](../backend/app/main.py#L3265)、[重启参数](../backend/app/main.py#L3053)、[重启服务](../backend/app/main.py#L3122)、[存储重启](../backend/app/storage_db.py#L964)。默认免费重译上限为 `-1`，见 [重译配置](../backend/app/domain/translation_qa_service.py#L44)。

**离线验证：** 构造无付款证明的待确认任务；取消及重启均返回 200，重启后为 `pending`，mock 入队调用 1 次，模型/质量改为 Pro/文学档，金额没有相应变化。未调用任何支付或模型接口。

**建议：** 将 `Order/Payment/Entitlement` 与 `Job/Attempt` 分开；所有重启先验证已付或明确赠送权益，冻结已购 `TranslationPlan`，升级另行报价。业务性免费重译可以保留，但必须有明确权益和预算。

### R2. 章节中间文件只用 basename，可能交付重复或串写的章节

**触发与影响：** 合法 EPUB 同时包含 `part1/chapter.xhtml` 和 `part2/chapter.xhtml` 时，两章会写入同一个文件。打包时可能把后一章内容回填到两处；若译文都是中文，EPUBCheck 和原文残留检查未必发现错章。

**证据：** [章节存储键与读写](../backend/app/domain/book_reduce_service.py#L23)、[主流程实际写入](../backend/app/domain/fast_translation_runner.py#L669)、[打包读取与覆盖](../backend/app/domain/book_reduce_service.py#L119)。Manifest 保留完整路径，丢目录发生在中间文件存储，不是输入已经扁平化。

**离线验证：** 临时目录中分别写入两条不同目录同名路径，返回的磁盘路径相同；读取第一章得到第二章正文。两次独立最小复现结论一致；没有生成或修改用户成品。

**建议：** 使用规范化 EPUB 完整路径的哈希或安全保留层级的键；中间产物增加 attempt 命名空间、原子写入和映射清单。最终审计增加“原章节身份→目标章节内容”的一致性，不能只查英文残留。

### R3. 转换附加 AI 精校已经计价，但没有接入执行链

**触发与影响：** 普通转换选择 `enable_precision_polish` 会增加费用，实际仍只执行普通转换；不会调用 `LLMPolisher`。这是售卖能力与实际履约不一致，不是精校效果欠佳。

**证据：** [附加收费](../backend/app/main.py#L1601)、[执行时仅开启用量 scope](../backend/app/job_runner.py#L358)、[转换调用参数](../backend/app/job_runner.py#L373)、[编译器参数与清洗器](../backend/app/engine/compiler.py#L62)。全仓库检索未发现 `LLMPolisher` 的运行时实例化，仅有定义、导出和文档示例。

**离线验证：** 检查真实 converter/compiler 签名和清洗器构造，两者没有精校参数，清洗器中没有精校器。不能用报价单元测试通过来代表精校已执行。

**建议：** 将付费能力落实为可审计的流水线步骤，并记录执行/成功/失败。实现前应停止售卖该未履约能力；修复时补“已收费→精校确实被调用→输出变更经过 QA”的契约测试。不要把翻译文学档的编辑阶段与转换附加精校混为一谈。

### R4. DOCX/Markdown 的高质量、文学档收费与实际翻译流程不一致

**触发与影响：** DOCX/Markdown 可选择更高质量档并按该档计价，但 `job_runner` 只把 EPUB 分发到快翻译流水线。旧转换器内部生成临时 EPUB 后继续走旧编译器，不会回到快路径；quality、cache policy、文体策略没有传递，缺少风险复核或文学编辑。关闭 `EPUB_FAST_TRANSLATION` 时，EPUB 也有同类退化。

**证据：** [格式策略](../backend/app/domain/input_formats.py#L3)、[质量档计价](../backend/app/main.py#L1575)、[分流与参数](../backend/app/job_runner.py#L362)、[旧翻译器构造](../backend/app/engine/compiler.py#L108)、[翻译器默认值](../backend/app/engine/cleaners/semantics_translator.py#L326)。

**离线验证：** mock 翻译器并执行真实构造，传入的仅是目标语言、双语、术语、温度、模型；其余退回 `standard / reuse / neutral_faithful`。没有调用真实模型。

**建议：** 在任务编排入口统一 `NormalizeInput → TranslationPlan → 同一个翻译执行器`。无法支持的组合应在报价前拒绝或明确降档并重新报价，不应静默按高档收费、按标准流程执行。

### R5. 付款确认和消息投递不原子，Broker 故障后任务可能永久 pending

**触发与影响：** 数据库先提交 `pending_payment → pending`，随后才 `delay()`。中间崩溃或 Broker 不可用时没有入队；后续回调因已处理直接成功。主动 recover 和定时 reconcile 只扫描 `pending_payment`，无法补发这个 `pending` 任务。批量逐个发布也应纳入同一补偿设计。

**证据：** [已付状态提交](../backend/app/storage_db.py#L692)、[回调内随后发布](../backend/app/main.py#L3565)、[recover 状态限制](../backend/app/main.py#L3195)、[对账发布异常仅记录](../backend/app/tasks/reconcile.py#L135)。

**离线验证：** 隔离 SQLite 中给队列发布注入 `ConnectionError`；第一次后为 `pending`，第二次处理不再尝试发布，待付款扫描返回 0。证明现有多路查单没有覆盖“已付但未投递”。

**建议：** 同一事务提交付款事实和任务 dispatch outbox，由重试分发器投递；沿用 attempt/执行租约承受至少一次投递，批量为每个子任务记录独立投递意图。已有邮件 outbox 不能代替任务 outbox。

### R6. 超时关单只取消本地状态，迟到的真实付款可能不履约

**触发与影响：** 对账把超过两小时且网关仍 `WAIT_BUYER_PAY` 的任务本地改为 `cancelled`，未向支付宝关闭交易。用户若随后付成功，付款 CAS 因状态不再是 `pending_payment` 而失败，回调仍返回 success，任务没有执行。收款事件可能被记录，但不会自动履约或退款。

**证据：** [本地超时关单](../backend/app/tasks/reconcile.py#L89)、[本地取消](../backend/app/storage_db.py#L766)、[支付订单创建参数](../backend/app/infra/alipay.py#L129)、[迟到回调被忽略](../backend/app/main.py#L3565)。适配器没有配套网关关单调用。

**离线验证：** 三小时前未付订单对账后变 cancelled；再模拟网关付款成功，下一次对账 checked=0，付款 CAS 为 false，入队为 0。

**建议：** 明确网关关闭确认、本地过期和真实付款的优先级；成功付款不能被执行状态吞掉。竞态出现后必须进入履约或可见的退款/人工处理状态。

### R7. Worker 子进程丢失后，可能 ACK 消息但留下永久 running

**触发与影响：** Celery 已启用 late ack，但没有为 worker-lost 配置重新入队。pool 子进程遭 OOM/SIGKILL、父 Worker 仍活着时，默认可能确认消费，业务函数无法执行清理；主应用没有失联 running 扫描恢复器。硬时限强杀也需纳入验证。

**证据：** [Celery 配置](../backend/app/infra/celery_app.py#L63)、[整书任务重试只覆盖租约异常](../backend/app/tasks/job_pipeline.py#L13)、[对账只扫描待付款](../backend/app/tasks/reconcile.py#L42)。本机解析配置为 `acks_late=True`、`acks_on_failure_or_timeout=True`、`reject_on_worker_lost=None`，并核对了本机已安装 Celery 的 worker request 丢失处理分支。

**验证边界：** 属于代码、配置与本机依赖实现验证；没有强杀生产进程或执行真实 OOM 测试。

**建议：** 整书任务配置 worker-lost 恢复，并增加 DB 心跳、租约失效后的恢复扫描、最大恢复次数及人工兜底。不能只打开无限重投，否则毒性书稿可能重复崩溃、消耗费用。

## 3. P2：可靠性、扩容和入口一致性

### R8. 状态/attempt 守卫是读后写，缺少数据库级并发隔离

在 [update_status](../backend/app/storage_db.py#L813) 中先读取并检查 attempt 和 cancelled，随后 ORM 按主键写回，提交不带版本/状态条件。离线双 Session 屏障测试确认：旧 writer 读到 running 后暂停，另一 writer 取消，再放行旧 writer，最终仍变 success。执行租约检查不能与这次 DB commit 组成原子操作。

建议将状态、版本、attempt 放进 `UPDATE ... WHERE ...` 条件，以影响行数决定是否拥有写权限；章节/chunk 等关联写入同样采用 attempt fencing。不要继续把现有检查描述为“绝不发生旧任务覆盖”。

### R9. 长翻译任务可能使当天对账消息等待至过期

[默认配置](../backend/app/infra/celery_app.py#L41) 整书并发为 1、软/硬时限为 7200/7500 秒，[对账与余额任务](../backend/app/infra/celery_app.py#L77) 的消息过期时间却为 3600 秒，且没有独立队列路由。若凌晨对账消息前方还有超过一小时的翻译工作，该消息消费前便可能过期；prefetch=1 不能解决。

建议增加独立 housekeeping 队列和轻量消费者，书任务继续有界并发。完成邮件目前已在 API 独立线程处理，不应错误地算进这条 Celery 队列。

### R10. 独立修复有持久化，但执行隔离仍只有进程级

[缓存、锁与 active 集合](../backend/app/main.py#L3766) 是进程内的；[读取订单](../backend/app/main.py#L4032) 只在首次缺缓存时读 JSON；[启动修复](../backend/app/main.py#L4083) 每单新建线程，并使用固定临时成品路径。增加 API workers 后会产生陈旧缓存、重复执行及文件争抢；单进程下也没有统一线程总并发上限。每次 tick 最多恢复 3 单不是全局执行上限。

建议迁入统一 JobStore、工作队列和租约；迁移前明确单 API 进程约束，增加有界执行器。已有 order.json 原子替换解决的是单次写入完整性，不解决多进程一致性。

### R11. 旧 SEO 工具页仍走 PayPal 时代结账流程

[epub-translator.html](../frontend/epub-translator.html#L1548)、[vertical-to-horizontal.html](../frontend/vertical-to-horizontal.html#L1551)、[traditional-to-simplified.html](../frontend/traditional-to-simplified.html#L1620) 创建任务后直接显示 running 并轮询，没有处理当前支付宝返回的 pending_payment/pay_url/qr_code；还保留旧 PayPal 创建与捕获代码。FastAPI 静态挂载仍可服务这些文件。

建议复用统一结账组件或将工具操作跳转到主页，SEO 内容可保留。此次未核验生产 Nginx 是否额外配置重定向，因此这是仓库入口缺陷，不是已复现的线上用户故障。

### R12. 可选全局限流没有覆盖全部模型调用

[正文翻译器](../backend/app/engine/cleaners/semantics_translator.py#L1404) 获取令牌；[Book Profiler](../backend/app/domain/book_profile_service.py#L467)、[术语抽取](../backend/app/engine/glossary_extractor.py#L357) 则直接由费用包装调用 SDK，没有经过同一令牌桶和共享路由健康。并行上传预分析时，即使启用全局 RPM/TPM，仍有请求绕过限制。该功能本身默认关闭，本次没有核验生产启用状态。

建议将白名单、连接复用、限流、健康路由、预算、重试和费用账本收口到统一 LLM 调用层，所有 stage 使用同一接口。支付前分析还应有单书/用户级费用预算与去重，避免用户反复上传造成不受控的前置成本。

### R13. 全站通知列表缺少鉴权和分页

[通知接口](../backend/app/main.py#L3296) 没有接收 Request 或调用任务访问校验，省略 job_id 就读取全部通知；[存储查询](../backend/app/storage_db.py#L1152) 用 `.all()`。通知 payload 已去掉书名、路径和下载 token，不能声称它直接泄漏书稿；但任务 ID、完成状态、错误类别和时间仍可被匿名枚举，历史增长后还有全表读取成本。

建议按登录用户或有效 Job Token 限定范围，强制分页与大小上限；无身份时不返回全站数据。

## 4. 演进顺序：先闭环，再拆模块

| 顺序 | 目标 | 涉及发现 | 完成标准 |
|---|---|---|---|
| 先止损 | 防绕付、停止售卖未执行能力、阻止静默错章 | R1–R4 | 所售档位与执行步骤一致，未付不能执行，章节身份不碰撞 |
| 可靠履约 | 支付事实/权益独立、dispatch outbox、晚到付款补偿 | R1、R5、R6 | 付款后任一步骤崩溃均有可追踪的执行或补偿结果 |
| 可恢复执行 | 原子 fencing、失联扫描、独立短任务队列 | R7–R9 | 取消/重启/强杀/重复投递不产生错误覆盖或永久悬挂 |
| 收敛分支 | 修复入统一队列、所有格式复用 TranslationPlan、统一结账和 LLM 调用层 | R4、R10–R12 | 不同入口不再出现权益、质量、限流差异 |
| 安全和扩容 | 通知权限、明确文件/缓存存储、健康与积压监测 | R13 与存储约束 | 身份边界可测，扩容不依赖隐含本机状态 |

建议内部边界为 `Orders/Payments`、`Jobs/Attempts`、`TranslationPlan`、`LLM Gateway`、`Artifacts/QA`、`Notifications`。先在同一工程内形成可测试模块，不需要立即增加多个网络服务、Kubernetes 或章节级 Celery 分布式执行。

当前 `main.py` 4426 行、`semantics_translator.py` 2880 行、`frontend/index.html` 4241 行，文件大是维护信号，真正应优先拆的是重复业务规则和状态所有权，不是机械按行数拆文件。

## 5. 回归验收清单

以下为修复时必须执行的验收项，**不是本轮已完成的全量回归**。本轮完成的是上文明确标注的隔离最小复现、签名/构造验证与账本正向测试。

- [ ] R1：未付取消后重启必须拒绝；已付原档重译可按策略执行；升级需新权益；并发重启只产生一个有效 attempt。
- [ ] R2：包含跨目录同名 XHTML 的真实 EPUB，从翻译回写到最终 ZIP 检查各章文本哈希、TOC 与 href；两次重译不能读到旧 attempt 中间文件。
- [ ] R3：精校开关关闭时零精校调用，打开时确实执行；失败时不能显示“精校完成”；报价、账本和输出报告一致。
- [ ] R4：EPUB/DOCX/MD × standard/high/literary × reuse/verified/fresh 的执行计划契约；取消和重启同样覆盖。
- [ ] R5：在付款提交后、发布前/后分别注入异常；重复回调、批量部分发布失败、Redis 暂停与恢复后最终均可履约且不重复计费。
- [ ] R6：模拟超时关单与付款同时发生，迟到通知/主动查单均不能丢失实付订单。
- [ ] R7：隔离测试 Worker 子进程强杀、软/硬超时、父进程重启；检查恢复上限、检查点复用和最终可下载性。
- [ ] R8：双 Session 并发取消/完成、取消后新 attempt/旧 writer、章节与统计写入，旧身份不能再成功提交。
- [ ] R9：长书占满 book 队列时，对账仍按时执行且不过期；短任务故障不阻塞翻译。
- [ ] R10：修复双 API 进程、重复付款通知、重启恢复和大量已付任务；保持一次有效执行并限制总并发。
- [ ] R11：主页及三个 SEO 页的未付、已付、取消、刷新恢复和下载端到端测试，禁止支付流程各自分叉。
- [ ] R12：同时触发画像、术语、正文、补译和编辑，所有调用共享 RPM/TPM；验证 Redis 故障策略和预算耗尽行为。
- [ ] R13：匿名不可列全站通知；不同用户/令牌相互隔离；分页有稳定游标和上限。
- [ ] 发布前：使用留存回归书稿做成品内容映射检查，确认不是只过 EPUBCheck；发布后验证用户刷新能找到原订单及新成品。

后续线上核验应单独确认生产提交、环境开关、Worker 配置、历史受影响订单和文件留存，不能用本轮本地审查替代。
