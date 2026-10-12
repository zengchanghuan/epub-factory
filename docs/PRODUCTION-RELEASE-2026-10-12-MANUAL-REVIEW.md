# 2026-10-12 生产发布验证：人工排查提示与翻译说明

## 结论与版本

源码提交 `6a6fc1ed7571cfbe6c6908d5b578d251a2ff6f05`（`fix: restore manual review guidance and translation copy`）通过 CI 后，已部署到 <https://fixepub.com>。真实生产 API 和隔离 Chrome 验证确认两处修复生效：

- 需要人工排查的任务保留联系客服、提供订单号及无需重新付款的说明，重译按钮禁用；普通 QA 检查结果不会覆盖该提示。
- 首页翻译功能说明及 FAQ 结构化数据恢复审慎表述，说明译文适合辅助阅读，并提醒专业术语和重要内容结合原文复核。

结构化证据见 [verification/2026-10-12-manual-review-release.json](verification/2026-10-12-manual-review-release.json)。其中 CI 数据来自 GitHub Actions，浏览器数据来自本轮原始脱敏报告，部署与健康检查摘要来自当轮执行输出；不包含访问令牌、会话凭据、配置文件内容或用户书籍。

本文记录已经完成的发布。此次文档提交仅归档结果，不重新部署应用，生产源码版本仍为 `6a6fc1e`。此前 `967e8b2` 的重复上传恢复和批量下载验证另见 [PRODUCTION-E2E-2026-10-12.md](PRODUCTION-E2E-2026-10-12.md)，不计作本轮重新实测。

## CI 结果

[GitHub Actions run 38198240825](https://github.com/zengchanghuan/epub-factory/actions/runs/38198240825) 的 `headSha` 与发布提交完全一致，最终状态为 `completed / success`。北京时间 2026-10-12 11:03:22 创建，11:13:33 更新为完成；无需追加修复。

| Job | 结果 | 覆盖范围 | 执行时间（北京时间） |
| --- | --- | --- | --- |
| `frontend` | success | 前端回归、静态工具页及 SEO 检查、隔离真实 Chrome 测试 | 11:03:26–11:04:47 |
| `regression` | success | 构建生产同环境镜像、容器离线回归、实际 Redis 6 和 7 的配额 Lua 验证 | 11:03:26–11:13:33 |

该提交在 `frontend/tests/test_f19_provider_pause.js` 中补充人工排查提示的回归，在 `frontend/tests/test_f31_translation_copy.js` 中覆盖首页及 FAQ 文案。

## 生产部署与健康检查

从该提交的独立干净副本运行 `bash deploy.sh`，发布包包含 548 个源码文件（2,046,653 字节），逐文件核对与 Git 提交一致。运行时数据及凭据未进入发布包。

- 部署前检查通过：Nginx 配置有效，worker 角色正确，无排队／运行任务或持久化活跃修复任务。
- 维护前验证发布内容，备份生产数据与配置，随后完成发布和健康检查，部署命令退出码为 0。
- 本次备份目录：`/home/ubuntu/epub-factory/deploy-backups/20261012T031548Z-3150201`（目录时间为 UTC，对应北京时间 11:15:48）。
- 备份中的 `previous-code.zip`、`production.env`、`jobs.sqlite3`、`pip-freeze.txt` 均存在；私有备份留在服务器，不纳入 Git。
- 部署后清单核对：548 个文件全部匹配，差异为 0。
- 公网首页、`lib.js`、`pdf-conversion.js`、`epub-translator.html`、`about.html` 均返回 200，内容与发布提交一致，缓存策略为 `no-cache, must-revalidate`。
- `/api/healthz` 返回 200 / `ok`，`/api/v2/capabilities` 和独立匿名会话的 `/api/v2/jobs` 返回 200；任务列表为 `Cache-Control: no-store`。
- API、转换 worker、housekeeping worker、beat 四个服务均为 `active/running`，`NRestarts=0`。
- 两个 worker 均响应 `pong`，分别消费 `celery` 和 `housekeeping` 队列。
- 从本次备份时间至部署后复核，上述四个服务的 systemd error 级别日志条目为 0。

## 线上验证方法与结果

运行标识为 `e2e-manual-review-6a6fc1e-8b45a924`。浏览器验证时间为北京时间 2026-10-12 11:16:54–11:17:02，7 项检查全部通过；两张 QA 提示截图另经视觉核对。

### 人工排查提示

在生产存储中创建两条独立的合成测试记录，均标记 `is_test_order=True`，任务状态为终态 `failed`，无支付权益、无派发记录。两条记录分别使用 `failed`、`blocked` QA 状态及非空检查列表，免费重试次数达到上限，使实际后端计算出 `manual_review_required=true`、`retryable=false`。

| 测试任务 ID | QA 状态 | 错误码 | 浏览器结果 |
| --- | --- | --- | --- |
| `d5d8b6bbabd6` | `failed` | `PARTIAL_TRANSLATION` | 人工排查说明正确，重译禁用 |
| `941bc232543a` | `blocked` | `TRANSLATION_PROVIDER_UNAVAILABLE` | 人工排查说明优先于普通阻塞提示，重译禁用 |

使用隔离 Chrome 配置目录和这两条测试记录自己的访问凭据，通过生产 HTTPS 页面访问真实 API，未拦截或伪造接口响应。两种场景均确认：

1. 实际 API 返回 200、人工排查标记及非空 QA 检查列表。
2. 页面标题为“任务需要人工排查”。
3. 说明为“该任务需要人工排查，请联系客服并提供订单号；无需重新付款。”，与后端返回值一致。
4. 原 QA 检查项仍显示，重译按钮禁用，文案为“当前不可重译”。
5. 提示区域实际可见，浏览器无未捕获 JavaScript 异常，记录到的 API 响应均为 200。

这是合成失败状态下的真实生产 API→页面验证，没有人为触发供应商故障，没有执行 AI 翻译、真实扣款或修改客户任务。空 summary 的兜底提示、陈旧 `retryable` 标记等其他边界以回归测试为依据，不计入线上实测。

测试记录保留在生产存储；合成文件及私有测试状态保留在服务器本轮 `deploy-backups/e2e-manual-review-6a6fc1e-8b45a924` 目录供追溯；本地临时凭据和隔离 Chrome 配置目录已删除。

### 首页翻译说明

真实浏览器读取首页可见功能卡片及 JSON-LD FAQ，确认两者均包含“专业术语和重要内容建议结合原文复核”，不再含“文学级翻译”或“深度理解语义”。功能卡片标题为“整本翻译与双语对照”，正文包含“译文适合辅助阅读”。这验证了已发布的文案与结构化数据，不代表评测实际译文质量。

## Stash 保留记录

本轮 CI 检查、部署、线上验证及文档整理未执行 stash 应用或删除操作，两份本地备份的对象 SHA 保持不变：

| 引用 | 名称 | 对象 SHA |
| --- | --- | --- |
| `stash@{0}` | `codex-before-pull-20261012` | `1e46c562877ebe306080d42df0bab4eca5d615a8` |
| `stash@{1}` | `codex-before-pull-20260918` | `73afa55dd291ad4a4fa95684f358072011261588` |

普通 `git push` 不同步 stash；本次保留的是本地两份原始备份。
