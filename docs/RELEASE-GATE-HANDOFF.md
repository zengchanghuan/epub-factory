---
title: 隔离回归与双 Mac 交接
date: 2026-10-02
status: local-verified-second-mac-deferred
---

# 隔离回归与双 Mac 交接

本流程将发布前回归从工作区运行数据中隔离出来，不改变模块化单体或业务处理规则。上一阶段 D55 已提交推送 `1970d76`，尚未部署。用户明确第二台 Mac 关机、实测后做；本文不代表双机已验收，也不授权部署、付费模型调用或开启 PDF。

## Action Plan

- [x] 推送已通过真实三书回归的 D55，确认本地与远端提交一致。
- [x] 完成本机可携带的隔离入口及其安全契约测试：运行时 20/20、门禁 38/38，0 跳过。
- [x] 同一冻结副本执行完整后端 catalog、前端和三本真实书门禁：104/104 后端脚本、33 套 / 284 前端用例、三书 10/10；既有 7 个可选用例跳过单列，真实三书 0 跳过。
- [ ] 第二台 Mac 开机后同步同一提交，自行准备依赖及私有样本，并实际重跑；按用户要求延期。
- [ ] 两台结果逐项核对后关闭双机验收；发布另按部署流程授权与预检。

## 门禁边界

`scripts/release-gate.py` 从 Git 管理及未忽略文件中筛选白名单源码，复制到仓库外的新私有证据目录。它不导入业务应用、不加载 `.env`、不复制私有配置、订单库、密钥、上传书稿、旧成品或运行锁。只对已跟踪的公开占位模板 `backend/.env.example` 设精确路径例外（D23 必须读取它），不允许其它 `.env*` 或未跟踪同名文件。拒绝复用既有证据目录，拒绝仓库内目录及符号链接源文件。

子进程使用限定环境、临时 HOME/数据库/缓存/修复目录和假密钥；旧 C1–C3 的输出改为各自临时目录，不再共用固定 `/tmp/test_*`。全量 catalog 需要的合成 `test_en.epub` 仅作为隔离运行夹具生成，不替代真实历史书。源码冻结后不为凑通过率改测试或业务代码；文件哈希变化使门禁失败。

Python 父子进程安装网络审计守卫，拒绝连接、DNS 查询及数据报发送，日志不保存目标地址或凭据；禁止 dotenv 自动发现或读取原工作区配置。仅允许显式读取本轮固定临时根内、测试自己生成的无符号链接普通配置文件，以保留 Worker 启动配置的真实测试语义；子进程改变 `TMPDIR` 不能扩大该根。此机制是测试护栏，**不是操作系统级沙箱**，不保证 Java/Node 或原生扩展所有外网访问均被拦截，也不用于执行不可信代码。超时只终止门禁自己启动的进程组，保留失败证据；不停止用户已有服务。

本门禁不连接生产 Redis、支付宝或模型 API，不发送邮件，不安装依赖。D55 的网关和 broker 传输是受控替身，真实执行的是 EPUB 转换、状态恢复及交付链；Redis/Lua/prefork/systemd 的真实证据另见 [D54 验收](INFRA-AND-PDF-EXECUTION-2026-10-02.md)。不把两种验证混为一次。

## 每台 Mac 独立准备

先保存本机改动；只在干净或已妥善处理的工作区 `git pull --ff-only`，不要覆盖未提交文件。两台机器必须使用同一已提交版本；若测试含未提交源文件，报告要保留 dirty 标记及完整源码 SHA，不声称可仅凭 commit 复现。

运行环境基线：

| 项目 | 要求与记录 |
|---|---|
| Python | 显式选择 3.10.12；保留 venv 入口路径，不把符号链接解析成基础 Python 后运行 |
| Python 依赖 | `backend/requirements.lock` 中每个固定版本必须匹配；额外开发包允许存在 |
| Node.js | 22.x；可传绝对路径，不依赖交互 shell 自动切换 |
| Java | 17；通过实际可执行程序版本检查 |
| EPUBCheck | 显式提供 5.1.0 JAR，检查版本并记录 SHA |
| 原生库 | 记录实际 SQLite、lxml、编译/运行时 libxml2 等；Python 包锁不锁定这些库 |

门禁不自动修复环境。缺少依赖时先在该 Mac 的专用 venv 按锁文件准备，不复制另一台 Mac 的 `.venv`、私钥、生产 `.env` 或数据库；不以升级全部依赖绕过版本差异。

若本机默认 Java 不是 17，先确认已有 Java 17 的安装位置，只在这次命令前设置 `PATH`（例如 Apple Silicon Homebrew 的 `PATH="/opt/homebrew/opt/openjdk@17/bin:$PATH"`）。另一台 Mac 应自行核对路径，不能照搬；不修改系统默认 Java 或 shell 启动文件。Node 也可通过 `--node` 指定本机绝对路径。

已知限制：本机 Apple SQLite 3.54.0 的真实 Celery prefork 组合曾 SIGSEGV。普通离线/D55 门禁通过不能证明该问题修复；此前 D54 采用仅作用于测试进程的生产同版 SQLite 3.37.2 完成验证。新门禁不继承 `DYLD_LIBRARY_PATH`，不替换系统库，也不擅自将该临时路径固化成另一台 Mac 的配置。

## 执行命令

以下均从仓库根目录执行。输出目录必须不存在；再次运行请换新名字，不覆盖上一轮失败日志。

```bash
backend/.venv/bin/python scripts/release-gate.py \
  --python "$PWD/backend/.venv/bin/python" \
  --node node \
  --epubcheck-jar "$PWD/tools/epubcheck-5.1.0/epubcheck.jar" \
  --evidence-dir /private/tmp/fixepub-offline-new-run \
  --profile offline
```

`offline` 只运行完整现有后端 catalog 与全部前端 `test_*.js`。既有可选历史用例允许且必须报告的跳过为 D21×2、D26×1、D27×1、D28×1、D29×2；不将这些缺失样本算作真实文件通过。新增/超额跳过、脚本非零退出、超时或源文件漂移均失败。

`history` 在同一快照中运行以上全部测试，再执行 D55 真实书门禁。先在当前机器设置三份实际目录，再运行：

```bash
# 三个变量须由操作者明确设为当前机器的真实目录，不复制别人的绝对路径。
: "${HISTORY_UPLOADS:?请指定原稿目录}"
: "${HISTORY_OUTPUTS:?请指定旧成品目录}"
: "${HISTORY_BASELINES:?请指定严格转换基线目录}"

backend/.venv/bin/python scripts/release-gate.py \
  --python "$PWD/backend/.venv/bin/python" \
  --node node \
  --epubcheck-jar "$PWD/tools/epubcheck-5.1.0/epubcheck.jar" \
  --evidence-dir /private/tmp/fixepub-history-new-run \
  --profile history \
  --uploads "$HISTORY_UPLOADS" \
  --outputs "$HISTORY_OUTPUTS" \
  --baselines "$HISTORY_BASELINES"
```

真实书为《The Annotated and Illustrated Double Helix》《別把你的錢留到死》《責任與判斷》。原稿和旧成品 SHA 来自 `test_d37_entitlement_history.py` 的 `BOOKS`；三份基线来自 `test_d54_infra_history.py` 的 `BASELINE_SHA256`。预检只按 AST 读取固定数据，不导入测试或应用。任一缺失、哈希不匹配或符号链接不安全都失败，不自动下载、重建基线或用合成文件替换。

历史门禁实际运行三本普通转换及三本人工恢复履约转换，逐本检查 EPUBCheck、正文、图片字节、目录/链接/ID、状态重载与签名下载 SHA。历史专项必须 0 跳过，9 份输入及旧基线前后不变；不调用收费模型，不代表翻译语义质量验收。私有书稿和完整测试日志均不进入 Git。

新工具自身契约可独立执行（不导入应用，也不打开工作区业务库）：

```bash
backend/.venv/bin/python scripts/test_release_runtime.py
backend/.venv/bin/python scripts/test_release_gate.py
```

## 证据与放行

保留门禁生成的结构化报告、每项命令/退出码/超时/跳过信息、运行时和源码 SHA、历史样本 SHA 及脱敏网络审计。只有当前同一冻结快照完整成功才放行，不把多轮中不同的成功项拼接成通过。

本交接工具初次交付时，CI手写catalog与本地完整catalog不同。后续[CI清单一致性子项](CI-CATALOG-2026-10-02.md)改为读取同一个D/C清单并补入D17，共105脚本；它不等于完整统一执行器，也不声明GitHub CI已运行或覆盖私有历史样本。初版104脚本/138命令与后续105脚本/139命令门禁分别记录，不混用。

第二台 Mac 的执行结果尚无。待其开机后，核对同一 commit、锁文件/源 SHA、运行时原生库、9 份样本 SHA、完整测试结果与跳过项；如需要发布，再单独验证该机器自己的 SSH 和 `deploy.sh --check`。未完成前保留“待验收”，不自动进入文本 PDF 或像素 OCR。

## 本机执行记录

- 首轮完整证据：`/private/tmp/fixepub-release-20261002-local-01/report.json`，138 个命令中 137 通过、1 失败，整体判为失败；未拼接为成功。后端 D47 的临时 `.env` 被新守卫过度拦截，造成 4 个断言失败；没有为此改业务代码或测试断言。
- 同轮前端 33 套 / 284 项、D55 历史专项 10/10（55.530 秒、0 跳过）通过。9 份历史输入/旧成品/基线不变，受保护的工作区数据库及 WAL/SHM 哈希不变，源码无漂移，Python 网络审计 0 事件。完整门禁仍因 D47 失败而拒绝放行。
- 修复方向是仅允许固定受控临时根中的显式测试配置，补正反例后从新冻结副本重新执行全部门禁；不能复用首轮的其它通过项代替最终完整运行。

### 最终完整重跑

唯一最终证据：`/private/tmp/fixepub-release-20261002-local-02/report.json`，源码快照位于同目录 `source/`。**138/138 命令通过、0 超时，总耗时 281.966 秒**，不是拼接首轮结果：

- 后端完整 catalog **104/104**；既有可选样本 D21×2、D26、D27、D28、D29×2 共 7 个测试方法显式跳过，报告保留逐项名称和原因。
- 前端 **33 套、284/284**；必须观察到非空且失败数为 0 的结果，不以空脚本退出 0 视为通过。
- D55 真实三书 **10/10、0 跳过，57.173 秒**。重新实际执行三次严格普通转换和三次人工恢复履约转换，EPUBCheck、原图字节、正文、目录/链接/ID、状态重载和签名下载 SHA 均通过。
- 原稿、旧成品及基线 9 份文件前后 SHA 匹配；受保护的根目录/backend 订单、限额及译文缓存数据库（含存在的 WAL/SHM）哈希不变。没有打开真实工作区数据库执行 SQL。
- 445 份冻结源文件无漂移；主门禁 Python 网络审计 **0 事件**。同一快照另外执行工具自身契约：`runtime-selftest.log` **20/20**、`gate-selftest.log` **38/38**，均 0 跳过；这些工具反例会故意触发受阻网络事件，单独记录，不混入主门禁的零事件统计。
- 实际运行时：Python **3.10.12**、Node **22.21.1**、Java **17.0.16**、EPUBCheck **5.1.0**、SQLite **3.54.0**、lxml **5.1.0**、libxml2 **2.12.3**；锁文件包版本 **0 漂移**。SQLite 3.54.0 的真实 Redis/Celery prefork 既有问题不在本门禁验证范围内，仍未宣称修复。

最终运行后仅补记本交接文档、顺序验收记录和架构跟进文档；代码、测试、配置与锁文件内容保持冻结。新增 CI 契约注册及 YAML/diff 本机检查通过，不宣称已运行 GitHub CI。

交付状态：D55 已推送 `1970d76`；用户随后要求 `push and go on`，本阶段隔离工具与文档已提交推送 `d660b20f6a53256c0027a0028c74904e95499c67`，核对本地与远端一致，未部署。两个无关运行锁未提交。第二台 Mac 实测按用户要求延期，第三项整体仍不勾选完成；随后仅继续[文本 PDF 独立本地预检](TEXT-PDF-PREFLIGHT-2026-10-02.md)，公共入口和图片像素 TODO 不变。
