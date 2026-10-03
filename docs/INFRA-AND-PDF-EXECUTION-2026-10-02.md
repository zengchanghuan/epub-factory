---
title: 基础设施与文本 PDF 顺序验收
date: 2026-10-02
base_revision: 140cb3fa959d8f2e1a510e5896a203c994dccb3f
status: stage-3-pushed-second-mac-deferred-stage-4-real-pdf-validation
---

# 基础设施与文本 PDF 顺序验收

延续 [R13、报价与支付恢复记录](ARCHITECTURE-FOLLOWUP-2026-10-02.md)。用户已批准按以下顺序推进：每项必须先通过真实历史文件回归，才开始下一项。保留模块化单体；优先在既有基础设施、领域和适配器边界修复。

## Action Plan

- [x] 第一项：真实依赖及生产配置验收。用户 `go` 授权后完成排空、备份、Worker/JWT/持久目录迁移；追加修复真实并发建表故障，经三本历史书与完整回归后部署，公网刷新下载通过。供应商真实配额、另一台 Mac 和完整灾备边界仍按下文明确保留。
- [x] 第二项：异常订单人工处理闭环。明确展示待核验原因、证据、处理人和结果，不猜旧单通道、不自动退款；冻结快照 104/104 后端脚本、284/284 前端、三本真实书 10/10 门禁通过。已提交推送 `1970d76`，未部署。
- [ ] 第三项：冻结源码全量回归与双 Mac 交接。本机隔离入口已完成，唯一最终快照 104/104 后端脚本、284/284 前端、真实三书 10/10、工具专项 58/58 通过；公开 7 个既有可选跳过项，固化依赖、命令与样本 SHA。用户明确第二台 Mac 已关机、实测后做，不标记双机通过。
- [ ] 第四项：文本 PDF 接入。仅可靠文字层，图片保持不变；真实文本 PDF 验收通过后才开放入口，扫描 OCR 不在本轮范围。
- [ ] 第五项：后置运维与精确缓存折扣。先审计保留和清理边界；折扣必须有冻结且可验证的执行计划，不能恢复不可靠的命中率承诺。

## 验收约束

- 真实原稿、旧成品及基线只读，开始和结束核对 SHA；输出仅写隔离临时目录。
- 合成夹具、模型身份回放、支付传输替身分别标注，不冒充真实书稿或收费模型语义质量验收。
- 测试只访问明确的 loopback Redis；不调用真实支付宝、邮件或付费模型，不连接生产队列。
- 不因本地测试通过推断生产配置、第二台 Mac 或真实支付已验收。
- 用户已另行授权本项维护窗口：确认无运行任务、完整备份后迁移配置并部署重启。只关闭 FixEpub 的 API 入口，不停止同机 Nginx；不删除旧文件、不退款、不产生测试付费模型调用。其他项仍须分别通过门禁后推进。
- 图片像素识别、像素翻译及重绘仍为后续 TODO。

## 第一项：现场核对

- 三本真实历史书：`The Annotated and Illustrated Double Helix`、`別把錢留到死`、`責任與判斷`。三份原稿及三份旧成品存在且 SHA 与 D37 固定记录一致；三份基线也存在。完整 SHA 由回归报告记录。
- 本机最初无 Redis 服务端；Docker Desktop 启动需要管理员组件配置，因此不将容器启动失败当作 Redis 测试通过。采用临时 Redis 服务端作为隔离验收依赖，不安装系统常驻服务。
- 生产 `deploy.sh --check` 只读预检：Nginx 配置有效；因缺少 `epub-factory-housekeeping` systemd 服务而停止。尚未通过发布前置条件，未执行部署或重启。
- 后续项尚未开始。以下为维护前现场结论；维护执行与追加发现见后面的生产维护记录，不因局部验收通过提前放行第二项。

### 最终本地验收

- [x] `backend/test_d54_redis_integration.py`：Redis **6.0.16、7.2.10 各 14/14**，均 0 跳过。实际 Lua、四独立进程共桶、原子 RPM/TPM、模型别名共享、幂等结算、未知用量不退额度、超额负债、五调用阶段账本、真实 TCP 断开后 fail-closed 与恢复均通过。模型传输为受控替身，不是付费模型效果验证。
- [x] `backend/test_d54_infra_history.py` 最终冻结 **2/2、0 跳过，37.401 秒**：主代理独立使用 Python **3.10.12**、Redis **6.0.16**、SQLite **3.37.2** 重跑，与生产版本一致，但操作系统仍是本机 macOS，不宣称已完成 Linux/线上运行验收。
- [x] 三本真实原稿均实际生成横排简体 EPUB；另用《責任與判斷》执行 Worker 故障恢复，共四次实际转换。每份成品经过真实 Java EPUBCheck、图片字节、正文、ID、有效链接、目录层级/目标检查，Store 重建后刷新和下载 SHA 匹配。
- [x] 停止测试自有 Redis 后，三单发布失败仍保留各一条持久派发意图；实际重启 Redis 后恢复，重复消息不重复转换。书籍槽被占用时，独立维护 Worker 的对账、健康和余额任务仍完成。验款/余额响应受控，不连接真实支付与供应商。
- [x] SIGKILL 仅针对测试自有 Worker 进程组：活跃/尚有效租约均拒绝抢占；确认旧进程终止后，将已核对 owner 的测试 Redis 租约实际到期加速为 100 ms，沿现有恢复逻辑在同一 attempt 完成一次转换。这里使用受控过期时间，不声称实际等待了生产十分钟窗口。
- [x] 真实 SQLite online backup 后打开新 Store，验证原金额、状态、attempt、派发/执行记录和输出指针。有效签名 URL 无需额外 header 也能下载；裸路径、错签及过期签名必须 403。没有为错误的测试预期修改业务鉴权。
- [x] 三份原稿、三份旧成品、三份基线 SHA 均不变。父子 Python 进程审计 **121 次仅允许的 loopback 事件、0 越界事件**；最终夹具中工作区 `rate_limit.db` 及 WAL/SHM SHA 不变。不是操作系统级抓包，也不声称 Java 外网被 OS 防火墙隔离。
- [x] 只读审计脚本专项 **12/12**、Worker 部署契约 **25/25**、部署恢复契约 **6/6**；CI YAML 解析和 `git diff --check` 通过。

| 真实书稿 | 原图数量（保持字节不变） | 检查的文档数 |
|---|---:|---:|
| The Annotated and Illustrated Double Helix | 308 | 178 |
| 別把你的錢留到死 | 13 | 24 |
| 責任與判斷 | 1 | 19 |

原稿、旧成品完整 SHA 固定于 `backend/test_d37_entitlement_history.py` 的 `BOOKS`；本轮新成品 SHA 和实际临时路径记录于日志，未把书稿放入 Git。

### 失败记录与环境限制

- 本机原生 Apple SQLite **3.54.0** 在实际 Redis/Celery AsynPool 的 prefork 子进程首次连接时重复 SIGSEGV；`NOSETPS=1` 没有解决。简单 SQLite fork 小样本不能独立重现全部故障，因此不武断归因为所有 SQLite fork，也没有修改业务代码或换成 solo 池来绕过门禁。
- 同一冻结测试使用本机现有 Homebrew SQLite **3.53.2** 通过 2/2（37.743 秒）；进一步从官方源码在临时目录构建生产同版 **3.37.2** 后，主代理再次通过上述 2/2。仅为测试进程设置 `DYLD_LIBRARY_PATH`，未替换系统库、venv 或生产运行时。原 Apple 3.54.0 组合仍未修复，双 Mac 运行环境需要在第三项明确统一并各自验证。
- 初版夹具导入应用会打开固定路径的工作区免费额度 DB，而非隔离库。已在父/子应用导入前仅将该固定 SQLite 路径重定向到临时目录，保留真实 Schema 与代码。早期运行未看到主 DB 内容新增证据，但不能宣称完全未触碰 WAL/SHM；没有回滚/覆盖用户文件。修正后的最终门禁验证这三份文件 SHA 不变。
- 另一个初版失败来自误将“无 header 但带有效签名”的下载判定为未授权，已根据原协议补齐正反两组断言；保留失败日志，不把中间失败描述为已通过。
- CI 新增真实 Redis 6/7 限流步骤及只读审计专项；本地命令/路径/YAML 已验证，随后随 `2e65889` 推送，但本记录未核验 GitHub CI 结果。私有历史书仍在显式本地门禁中，不用合成书替代，也不上传 CI。

### 生产只读结论与维护条件

维护前证据：`/private/tmp/fixepub-d54-production-audit-final.json`。脚本不导入业务应用、不改业务记录、不执行 Redis 写操作或服务变更；SQLite `mode=ro` 可能维护 WAL 共享内存文件，不能宣传为绝对零文件写入，也不使用会漏读活跃 WAL 的 `immutable=1`。

- 实际 Python 3.10.12 / SQLite 3.37.2，订单库 WAL、`quick_check=ok`；Redis 6.0.16 三项配置均可 PING，API 与 book Worker 的相关配置一致。
- API、book Worker、beat 为 active；**housekeeping 服务不存在**，发布预检已拒绝部署。当前旧服务仍运行，不等于当前全站宕机。
- **JWT 未配置有效非占位密钥**。新版本登录保护会拒绝缺失/公开占位配置，需要在发布前补齐；密钥从未打印。更换密钥可能要求用户重新登录。
- 上传、成品、缓存/断点路径均存在且非临时目录；**修复数据仍在 `/tmp/epub-repair`**。需完整备份后迁到私有持久目录，不能只搬可见成品。
- 核对时没有 pending/running 书籍任务；修复持久元数据中有 7 个待付款、1 个已修复，另 37 个无元数据目录。不能把无元数据目录推断成未付款或可删除，也不能据此证明旧内存订单已安全持久化。维护前必须再次核对。
- 生产共享模型限流当前未启用，RPM/TPM 未设置；本轮验证代码，不擅自填写供应商配额或升级生产 Redis。启用时应按真实账号限额/经过确认的工程上限配置。
- 生产备份目录存在，但本轮只完成隔离库备份恢复，不宣称生产灾备演练已完成。

用户已用 `go` 批准维护窗口：先核对全部活跃书籍/修复和旧单，备份代码、unit、私有配置、订单/缓存库及全部修复目录，再迁移角色/JWT/持久目录并部署重启验证。第一项全部通过后才进入异常订单闭环。

### 已授权生产维护记录

- 维护前再次核对：136 个书籍订单，没有 confirming/pending/running；旧 Worker 的 active/reserved/scheduled 和 broker 队列均为空。37 个无元数据目录对应旧接口全部 404，另 8 个持久修复订单均可读；没有据此删除文件或伪造订单。
- 发布锁新增受校验的 FD 9 继承，父进程持锁覆盖配置迁移与标准部署。专项本机 **10/10**，服务器原生 Linux **10/10**；正常双 Mac 部署仍共用同一个锁 inode。
- 只在 FixEpub 的 Nginx `/api/` location 加临时门禁，没有停止共享 Nginx、其他站点或 `/bili-summary/`。首次立即探测遇到 reload 异步切换，安全恢复原配置且未停业务；后改为连续两次 503 才允许停机，实际探测为 `404 → 503 → 503`。
- 私有完整备份：`/home/ubuntu/epub-factory/deploy-backups/infra-maintenance-20261002T045024Z`，目录 0700。包括代码、完整 backend/venv、原 unit/Nginx/私有配置、SQLite online backup、修复目录及配置端点中的应用 `epub:*` Redis 键；不是共享 Redis 实例的全量灾备快照。原稿、成品及修复文件从 TAR 读回逐一校验 SHA，SQLite quick_check 通过。
- 修复目录完整复制到 `/home/ubuntu/epub-factory/runtime/repairs`，原 `/tmp/epub-repair` 保留。JWT 配置为新的非占位密钥，可能要求旧登录重新登录；Job access_token 与独立下载签名配置未替换。
- 部署首次启动暴露真实竞态：多服务同时 `create_all`，housekeeping 报 `table job_dispatch_outbox already exists`。标准部署判失败并保留 API 门禁，没有回滚任何订单库。Worker 自动重启成功后，独立完成全部连续性检查才恢复入口；并发建表缺陷继续作为第一项阻断修复，不能把自动重启当作根因已解决。
- 连续性通过：**136 个旧单金额/路径/token、终态状态不变；342 个源文件与成品 SHA 不变；8 个修复订单状态/价格及已完成下载不变；37 个旧不可读目录原样保留；3 个历史任务重新获取签名 URL 后下载 SHA 匹配**。源包 429 文件与服务器逐一匹配，归档 SHA `a3dc4c72ec3c4603b90118b3d379e3fb4fcbf7ad121bf3a7a7d911a4a79336dc`。
- 公网复核通过：`/api/healthz` 200；首页内容与发布源文件一致，`Cache-Control: no-cache, must-revalidate`；上述 3 个样本在真实公网 HTTPS 下刷新后均可下载且 SHA 一致。JWT 自签自验与错签拒绝通过，不冒充真实短信登录。
- 实际 Celery inspect 验证两个独立 prefork Worker：book 只消费 `celery`，housekeeping 只消费 `housekeeping`，各并发 1；四个 systemd 服务 active，Redis 三配置可 PING，订单库 `quick_check=ok`，API/Worker 关键配置一致。共享模型限流仍未启用，未擅自猜测供应商配额。

公网验收记录：`/private/tmp/fixepub-oct02-production-postverify.json`；维护后配置审计：`/private/tmp/fixepub-oct02-production-audit-after.json`。后者的 `backup_restore_verified=false` 指未做完整生产灾备恢复演练，不否定本次备份完整性/下载连续性验证。

### 并发初始化修复与追加真实验收

- 根因不是翻译内容：SQLAlchemy 的“检查表是否存在 → 建表”以及首次 WAL 连接没有跨进程互斥，首次升级时四个服务可竞争同一 SQLite。原发布源码的隔离四进程实验复现 `table already exists`，另一轮复现 `database is locked`。
- 仅在 `storage_db.py` 初始化边界加同数据库规范路径对应的永久私有 flock：从首次 WAL 连接到建表、列迁移和索引创建统一串行。直接调用兼容迁移也共用同一锁，正常任务读写不进入该锁；不吞迁移错误，不改变 PostgreSQL 与独立内存数据库路径。拒绝符号链接/非普通锁文件，锁等待有上限；此边界针对同主机本地 SQLite，不宣称 NFS 分布式锁。
- 冻结源码 SHA：`storage_db.py` 为 `bb8a16803096bcd3ac9fe3d00b64c53f2071701250cce674ad62b86afac1c92d`；`test_d54_schema_init.py` 为 `12b7485b5389e78051be6126f514ddb65c75518d1a2aab36ee6e324c559cc1c3`。
- 新专项本机 **17/17**，Linux 生产同款运行时的隔离目录 **17/17，8.222 秒**。覆盖真实四进程首次创建/升级、直接迁移、进程死亡释放、稳定 inode、错误传播、URI/内存数据库和路径安全；不打开生产订单库。关联通知/执行/权益/付款专项 **53/53**。
- 主代理在修复冻结后重跑真实历史 D54：**2/2、0 跳过，38.893 秒**；三本原稿及故障恢复共四次实际转换，EPUBCheck、原图字节、正文、目录/链接、签名下载、Redis 断连恢复及双 prefork 隔离再次通过。9 份原稿/旧成品/基线 SHA 不变，121 次允许的 loopback 事件，0 外部调用。第一轮被沙箱禁止 Redis bind，并非产品故障；经授权在同一受限测试网络策略下用新临时目录重跑，保留两次日志。
- 使用本次真实生产 `orders.sqlite3` 备份建立新的私有隔离恢复库，四进程同时初始化通过；新 Store 可逐一读取 **136 个真实历史订单**，SQL 原始金额/状态/输入输出路径/token 完全不变，原备份 SHA 不变、quick_check 通过。首个临时验证器误把历史 NULL 与领域模型既有的空串归一化视为差异；已按原映射修正比较，并补充原始 SQL 全等断言，没有为测试改业务规则或原数据库。这是应用订单库恢复验证，不是整机灾备/真实网关演练。

追加日志：`/private/tmp/fixepub-d54-schema-fixed-green.log`、`/private/tmp/fixepub-d54-schema-linux.log`、`/private/tmp/fixepub-d54-schema-fix-history-approved.log`、`/private/tmp/fixepub-d54-schema-real-backup-check-final.json`。

### 最终门禁与交付状态

- 修复后从唯一冻结副本完整执行 **102/102 个后端测试脚本，exit 0**，不是多轮结果拼接；新增 schema 专项 17/17，外网审计 0 次。记录：`/private/tmp/fixepub-d54-final-al5m50zn/backend-full.log`、`full/results.json`。7 个既有可选历史测试（D21×2、D26、D27、D28、D29×2）没有注入各自专用样本，显式跳过，不计入真实书验收；本项真实书证据是上面的 D54 四次实际转换。
- 前端、浏览器脚本 SHA 与已通过的 **32 suites / 263 项、Chrome 25 场景（0 JS 错误）**逐文件一致，复用原验收证据，没有宣称重新进行真实付款。CI 注册新增部署锁/初始化专项，YAML 与 diff 检查通过；随后随 `2e65889` 推送，GitHub CI 结果未在本记录核验。
- 并发初始化补丁通过标准四服务发布流程，退出码 0；标准备份 `/home/ubuntu/epub-factory/deploy-backups/20261002T050244Z-4060962`。最终发布包 **430 个源文件**，SHA `86fcd4d304a466c05e374478e58e5d7b36dfa9b35ba116b9bd3c0c226b64652c`，与服务器逐一匹配。
- 最终公网验收再次通过：首页源内容一致且 no-cache、健康接口 200、3 个历史任务刷新签名链接后下载 SHA 不变；136 个旧单金额/路径/token 不变，342 个历史资源 SHA 不变。四服务全为 active/running，`NRestarts=0`、`ExecMainStatus=0`；SQLite 私有 schema 锁存在且权限 0600，两角色各单队列/prefork/并发 1。
- 最终证据：`/private/tmp/fixepub-oct02-final-deployment.log`、`/private/tmp/fixepub-oct02-production-final-verification.json`。该 JSON 的 `continuity` 子对象保留第一轮 429 文件发布记录；顶层 `final_release_files_verified=430` 才是补丁后的最终发布，不能混为一次。
- 本项已经完成并部署；验收文档补记不涉及业务源码偏差。随后提交并推送 `2e65889dc5a56f5549c5bc4164e3d7332c5d4b1e`，确认本地与 `origin/main` 一致，再开始第二项。没有真实付款、退款或收费模型调用。第二台 Mac、Apple SQLite 3.54.0 prefork 问题以及真实模型语义质量不因此被宣称通过。

### 重跑与证据

限流脚本必须指定隔离 loopback Redis；不能使用生产 URL，缺少 opt-in 直接运行退出 2：

```bash
D54_REDIS_ISOLATED=1 D54_REDIS_URL=redis://127.0.0.1:16389/0 \
  PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python backend/test_d54_redis_integration.py
```

历史门禁自行启动/停止所提供的 Redis 可执行文件，只使用指定的未占用高端口。下面是本机实际重跑命令；临时产物目录须不存在，不能覆盖既有证据：

```bash
DYLD_LIBRARY_PATH=/private/tmp/fixepub-d54-sqlite-3.37.2/lib \
EPUB_INFRA_REDIS_SERVER=/private/tmp/fixepub-d54-redis-source/redis-6.0.16/src/redis-server \
EPUB_INFRA_REDIS_URL=redis://127.0.0.1:16390/0 \
EPUB_INFRA_ARTIFACT_DIR=/private/tmp/fixepub-d54-new-run \
EPUB_HISTORY_UPLOAD_DIR="$PWD/backend/uploads" \
EPUB_HISTORY_OUTPUT_DIR="$PWD/backend/outputs" \
EPUB_HISTORY_BASELINE_DIR=/private/tmp/fixepub-arch-20261001.RmFzTr/baseline-corpus \
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python backend/test_d54_infra_history.py
```

- 最终精确版本日志/产物：`/private/tmp/fixepub-d54-exact-runtime-final.log`、`/private/tmp/fixepub-d54-exact-runtime-final/`。
- Homebrew 对照日志：`/private/tmp/fixepub-d54-history-release-redis6.log`。
- 两版本限流日志：`/private/tmp/fixepub-d54-redis-6.0-acceptance.log`、`/private/tmp/fixepub-d54-redis-7.2-acceptance.log`。
- 审计契约：`/private/tmp/fixepub-d54-audit-contract-release.log`。
- 历史测试冻结 SHA：`80e55746c8066e4c389eae651965b08177b0c7601803a0899c7ddf2e29b68357`。
- Redis 官方下载归档 SHA：6.0.16 `3639bbf29aca1a1670de1ab2ce224d6511c63969e7e590d3cdf8f7888184fa19`；7.2.10 `e576ad54bc53770649c556933ecd555b975e3dac422e46356102436a437b43c7`。本机 Redis 6 编译附加 `-Dstat64=stat -Dfstat64=fstat -Dlstat64=lstat -DMAC_OS_X_VERSION_10_6=1060` 兼容当前 macOS SDK，不改 Redis Lua 源码。
- SQLite 官方 3.37.2 归档 SHA：`4089a8d9b467537b3f246f217b84cd76e00b1d1a971fe5aca1e30e230e46b2d8`。归档/编译均仅在 `/private/tmp`；记录下载字节哈希用于复现，不冒充独立供应链签名验证。

第一项已完成授权生产配置迁移、部署及 `2e65889` 推送，并在初始化架构边界修复了实际发现的竞态。图片像素处理继续保持 TODO，PDF 入口未提前开放。

## 第二项：异常订单人工处理闭环（D55）

### 实现边界

- 在现有模块化单体内增加 `admin/reviews.py`；使用主库的 `admin_order_reviews` 和只追加 `admin_order_review_events`，不新增服务或自动退款通道。表初始化复用同库跨进程 schema 锁，管理员会话表也进入此锁边界。
- 看板展示已付款待人工处理、原付款通道未知、模型费用缺口、原稿/购买档位不可验证及批次不一致等原因。工单筛选为“付款待人工处理 / 已登记待处理 / 已登记关闭”，不是先分页再筛选，也不假称已自动索引全部动态费用异常。
- 管理员可添加备注、登记外部已经完成的**整单全额**退款，或重新核验后恢复原单履约；费用/通道类跟进可以凭说明和证据关闭。仍处于 `paid_review` 的订单不能直接结案，以免隐藏尚未解决的付款责任。
- 履约必须重新查询可信网关响应，核对原商户订单号、原冻结金额及交易号，并确认费用与依据。AI 订单必须已有可验证的原购买方案，不猜旧档位、支付通道或改历史价格。整批状态/成员/金额不一致则拒绝，不能对子任务分别登记整批退款。
- 同一事务内提交新 attempt、清理当前章节/chunk 进度、持久投递意图和审计事件；保留原权益、缓存、费用历史、阶段历史和旧成品文件。即时投递失败仍保留 pending 和 outbox，后台可补发；相同请求编号重放不重复验款或创建 attempt。
- 每次操作校验工单 revision 和订单/原稿/账本上下文。并发或新事实会使旧确认失效；工单关闭只针对当时事实，不改通道或账本。已登记退款阻断普通重试、精校重试及重复付款回调释放。
- 退款操作**只是人工登记，不调用退款接口，也没有网关退款核验**。处理人由服务器登录会话确定；说明、证据、退款参考号只在管理员鉴权与 CSRF 保护的私有审计中返回，公开任务/批次状态不包含这些内容。历史产物路径只内部留存，不返回原始绝对路径。
- 前端使用服务端允许动作，提交防重、稳定请求 UUID、409 刷新和登录/详情异步响应隔离；备注、证据等只用文本节点渲染。没有操作生产订单或进行真实退款。

### 验收发现与处理记录

- 初版原稿检查错误拒绝 macOS `/var` 指向 `/private/var` 的上传根祖先别名；已接受规范根祖先别名，继续拒绝上传根内符号链接。
- 初版允许待解决的 `paid_review` 工单直接结案，导致后续无法履约/登记退款；已禁止此关闭路径。
- 初版新 attempt 未清理旧章节/chunk；已补入同一事务，增加清理与失败回滚断言。
- 独立核对发现非数字历史批次字段仍可能使管理员列表在反序列化时 500；已在管理员只读展示边界返回白名单异常摘要，禁用下载/核验/重试/处置等动作，服务端统一安全拒绝。正常子单遇到同批损坏成员也只显示诊断，不让整个列表失败；没有将坏元数据补猜为可执行订单或写回原行。
- 一次直接运行旧 D19 测试的付款邮件模块导入触发 `app.storage` 默认初始化，打开工作区根目录已有 `epub_jobs.db` 并执行兼容建表，生成永久 schema 锁。已披露并停止非隔离全量测试；保留该库和锁，没有擅自回滚。只读核对 `quick_check=ok`、853 个本机历史任务、新人工工单/事件和派发均为 0；没有运行前哈希，**不能声称本机库字节完全未变或证明所有历史列前后全等**。未访问生产库。后续完整 catalog 使用不含 `.env`/私有库/原稿的独立快照及外网阻断。

### 最终冻结验收

唯一最终快照：`/private/tmp/fixepub-d55-final-odefr4gn/current`；冻结源文件 SHA 清单：上一级 `source-sha256.json`。较早 `fixepub-d55-final-xnw_g79l` 的通过结果仅为发现元数据问题前的预检，**不与本轮拼接计算通过率**。

- 历史门禁 `test_d55_order_review_history.py`：**10/10、0 跳过，58.145 秒**。三本固定 SHA 真实原稿先实际普通转换建立严格内容基准，再分别走人工恢复履约产生新成品；不是合成 EPUB 或空跑模型接口。
- 每本书实际验证：上传后显式恢复历史用户取消态、可信迟到付款进入 `paid_review`、管理员登录/CSRF、原单验款、事务派发、模拟 broker 不可用/回执丢失、DB 重载、重复消息只执行一次、原请求在 pending/success 后重放不重复查单。历史取消态是受控恢复夹具，不宣称用户界面允许取消尚未付款订单。
- 三本人工履约成品通过真实 Java EPUBCheck **0 ERROR/FATAL**，正文、图片字节、ID、有效链接和目录层级/目标与严格转换基准一致；刷新状态后下载 SHA 等于新成品。原稿、旧成品、基线共 9 份 SHA 不变。
- 三书外部退款登记、权限/版本冲突场景通过；登记本身没有查款/退款/模型调用，后续重复付款回调、用户/管理员重试和刷新不会重新发放履约。公开详情不包含管理员身份、说明、证据和退款参考号。
- 本项**网关与 broker 传输是受控替身**，实际执行的是原 `run_job` 转换及交付链，不宣称再次完成真实 Redis/prefork/systemd 维护验收；那部分证据属于第一项 D54。Python 3.10.12 / SQLite 3.54.0 / libxml2 2.12.3 / EPUBCheck 5.1.0，不涉及 SQLite 3.54.0 prefork，因此不能据此宣称该既有环境问题已解决。
- 11 项网络/模型/SMTP 禁止调用守卫均未触发；工作区 `backend/rate_limit.db` 及 WAL/SHM 的 SHA 也由运行外层额外前后核对不变。未接入生产服务，不发生真实付款、退款、邮件或付费模型请求。
- 同一快照前端 **33 套、284/284**，其中人工订单 DOM 专项 **21/21**。覆盖退款明示、批次金额、防重复/幂等、迟到异步响应、CSRF、纯文本渲染和坏元数据界面禁用；不是浏览器真实付款验收。
- 同一快照完整后端 catalog **104/104 个脚本 exit 0**；新增 D55 存储 **28/28**、管理员 API **12/12**，初始化并发专项 **17/17**。7 个既有可选历史用例（D21×2、D26、D27、D28、D29×2）缺各自专用样本而明确跳过，不计作真实书通过；本项真实书证据为上面的 D55 独立 10/10、0 跳过门禁。
- 全 catalog Python 子进程继承外网拒绝审计，未产生网络事件日志；3 个既有 C 测试的固定 `/tmp/test_` 输出只在隔离副本中改指本次目录，修改前后哈希另存 `isolated-fixture-adjustments.json`，没有改变业务断言。完整结果保留在 `full/results.json`，不是多轮通过项拼接。
- 最终业务/测试/前端/脚本/CI 共 **396 个文件**与冻结清单逐一匹配；此后仅补记 Markdown 验收结果。CI YAML 解析、JS 语法和 `git diff --check` 通过；随后本项随 `1970d76` 推送，未核验 GitHub CI 结果，不宣称已在线执行。

日志：同一证据目录下 `history.log`、`frontend.log`、`backend-full.log`、`full/results.json`。第二项门禁已通过，可进入第三项冻结交付与双 Mac 交接；未在另一台 Mac 实际执行前不标记第三项完成。

回归命令（只读样本路径必须在各自机器明确配置；没有样本时不得把跳过算通过）：

```bash
EPUB_HISTORY_UPLOAD_DIR="$PWD/backend/uploads" \
EPUB_HISTORY_OUTPUT_DIR="$PWD/backend/outputs" \
EPUB_HISTORY_BASELINE_DIR=/private/tmp/fixepub-arch-20261001.RmFzTr/baseline-corpus \
EPUBCHECK_JAR="$PWD/tools/epubcheck-5.1.0/epubcheck.jar" \
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python backend/test_d55_order_review_history.py -v
```

第二项已提交推送 `1970d76fd9b4e27ead780d7a858ba8de3ec242ed`，核对 HEAD 与 `origin/main` 一致；未部署。第一项已部署的推送为 `2e65889`。本机额外 `.mcp-qq.lock` 和 schema 运行锁未纳入交付。

## 第三项：可携带的隔离回归入口

实现与重跑说明见 [隔离回归与双 Mac 交接](RELEASE-GATE-HANDOFF.md)。此次只处理测试基础设施与交付记录，不改业务架构，不部署，不开放 PDF，不处理图片像素。

用户已明确第二台 Mac 关机、实际验收后做。因此只完成本机可验证部分，双机验收保持未完成，不连接另一台机器；下一项文本 PDF 不因此提前放行。

- 统一入口 `scripts/release-gate.py`：私有源码快照、固定依赖/原生库版本记录、显式 9 样本 SHA、数据库隔离、Python 网络阻断、逐命令超时/日志/跳过记录和前后完整性检查。C1–C3 只将固定 `/tmp/test_*` 改为每次独立临时输出，业务和原断言不变。
- 首轮在 D47 暴露工具自身对测试 `.env` 的过度拦截，整体拒绝放行。仅在守卫边界允许受控临时根内显式、无符号链接普通配置；不读取工作区私有配置、不改 D47 断言。保留首轮证据，不拼接结果。
- 最终唯一快照 `/private/tmp/fixepub-release-20261002-local-02/source`：**138/138 命令、0 超时，281.966 秒**；后端 **104/104**，前端 **33 套 / 284 项**，D55 真实三书 **10/10、0 跳过，57.173 秒**，同快照工具专项另计 **20+38 项**。7 个既有可选历史用例跳过名称/原因单列，不冒充真实三书验收。
- 445 份源文件冻结核对通过，9 份真实原稿/旧成品/基线 SHA 不变；受保护的工作区订单/限额/缓存库及 WAL/SHM 哈希不变，主门禁 Python 网络事件 0。版本与完整边界详见交接文档。
- 最终验收后仅补记三份 Markdown 结果文档，代码、测试、配置与锁文件未变。随后按用户 `push and go on` 提交推送 `d660b20`，本地与远端一致，未部署；双 Mac、生产支付和付费模型语义质量不因本机通过而标记完成。

## 第四项：文本 PDF（分子项推进）

用户再次要求继续后，先做 [D56-A 独立本地预检](TEXT-PDF-PREFLIGHT-2026-10-02.md)。第二台 Mac 依然延期，不冒充完成；不依赖其开机来准备孤立代码，也不因此开放公共 PDF 入口或进行生产发布。

历史目录最初缺少可确认的真实 PDF 书稿，用户随后提供《西南联大逻辑通识课》，已完成 A 的[真实文件门禁](REMAINING-OPTIMIZATION-2026-10-02.md)，开始 B1 原始页/片段/编码资源映射。A 的合成契约和原 EPUB 回归不能替代 PDF 的真实文件门禁；B1 仍不放行正式报价或翻译链路。

D56-A 初版合成契约为29/29，原产品138/138命令与三书10/10，证据 `/private/tmp/fixepub-d56-preflight-final-01`；此为提供真实PDF前的历史记录。用户提供原稿后的最终记录为合成32/32、真实PDF4/4，同一冻结快照原产品138/138命令与三本EPUB10/10，证据 `/private/tmp/fixepub-d56-real-logic-final-01`。7个既有可选样本跳过单列；9份历史原稿/旧成品/基线与受保护DB/WAL/SHM哈希不变。本轮代码未提交、未部署，Linux资源隔离、段落/原图重建与正式翻译仍待验收，详情见D56-A记录。
