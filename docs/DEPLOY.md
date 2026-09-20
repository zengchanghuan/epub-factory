# 腾讯云部署

本工程的发布入口是根目录 `deploy.sh`。默认服务器为 `ubuntu@81.71.22.79:22`，工程目录 `/home/ubuntu/epub-factory`，网站 `https://fixepub.com`。不再依赖本机配置 `fixepub` SSH 别名或固定名称的私钥。

## 从本机部署

已提交的 `main` 分支可以一键推送并部署：`bash push.sh`。脚本要求工作区干净且 origin 推送地址为 `git@github.com:zengchanghuan/epub-factory.git`；Git 推送成功后才执行免密部署。部署失败时推送仍已完成，解决原因后运行 `bash deploy.sh` 即可。普通 `git push` 仍只推送代码；此入口不是 GitHub Actions 自动部署，也不会等待远端 CI。

```bash
# 首次安装部署公钥，需要在本机终端输入一次服务器密码。
bash scripts/setup-deploy-ssh.sh

# 后续免密部署：进入本机实际工程目录，不依赖用户名或固定目录。
bash deploy.sh

# 仅检查连接、服务器环境和运行中的任务，不发布。
bash deploy.sh --check

# 仅限已确认的旧版正常价修复订单：先保留旧订单，再发布。
# 必须已确认它不是测试单、启动后未修改旧修复价格；脚本还会严格核验旧版本。
bash deploy.sh --preserve-legacy-repair <32位任务ID>

# 使用已有 SSH 私钥。
DEPLOY_KEY=/绝对路径/fix_epub.pem bash deploy.sh

# 如需覆盖目标地址。
DEPLOY_HOST=ubuntu@81.71.22.79 DEPLOY_PORT=22 bash deploy.sh
```

本机需要 Python 3.9+、Git、OpenSSH 和 curl。SSH 首次连接时，请核对服务器主机指纹。服务器需要已有生产 `.env`、Python 虚拟环境、Java、EPUBCheck 5.1.0，以及三个 systemd 服务：`epub-factory`、`epub-factory-worker`、`epub-factory-beat`。这是已有服务的升级脚本，不负责首次建站或配置密钥。

免密安装脚本生成专用密钥 `~/.ssh/id_ed25519_fixepub`，只把公钥追加到服务器 `authorized_keys`，保留已有公钥；不保存密码、不修改 SSH 服务配置。私钥没有口令，保存在本机权限受限的 SSH 目录中，不进入工程或部署包。已有密钥可通过 `DEPLOY_KEY` 指定；带口令的密钥需先加入 ssh-agent。仅生成密钥可运行 `bash scripts/setup-deploy-ssh.sh --prepare`。部署入口启用 `BatchMode=yes` 和严格主机校验，认证异常会立即失败，不退回密码登录。

服务器执行 `sudo -n`：若账号没有免密 sudo，优先使用下面的腾讯云终端方式，在同一终端执行 `sudo -v` 后发布。脚本不会保存、传输文件形式的密码，也不会修改 sudo 权限。

首页和脚本由 Nginx 静态入口提供，须设置 `Cache-Control: no-cache, must-revalidate`，不能只依赖 FastAPI 中间件。发布后分别核验公网首页、带版本的 `lib.js` 和任务 API；任务接口应为 `no-store`。原下单浏览器的会话和任务令牌须保留，新浏览器/新会话不会自动获得历史订单权限。

入口重载前运行 `sudo /usr/sbin/nginx -t`。如证书与私钥不匹配，不直接重载当前仍正常运行的入口；先验证已安装证书的配对、域名和有效期，备份站点配置，再修正路径并通过校验。2026-09-17 已将本机站点证书指向与现有私钥匹配的 `/etc/nginx/ssl/fixepub.com.pem`，未更换密钥。

## 腾讯云网页终端部署

Chrome 已登录腾讯云时，可只在本机生成部署包：

```bash
bash deploy.sh --package-only
```

包生成于工程根目录 `epub-factory-deploy.zip`。通过 OrcaTerm 文件管理器上传到服务器 `/tmp/epub-factory-deploy.zip`，然后在服务器终端执行：

```bash
sudo -v
python3 -c 'import zipfile; z=zipfile.ZipFile("/tmp/epub-factory-deploy.zip"); open("/tmp/epub-deploy-server.sh","wb").write(z.read("scripts/deploy-server.sh"))'
bash /tmp/epub-deploy-server.sh /tmp/epub-factory-deploy.zip /home/ubuntu/epub-factory
curl -fsS https://fixepub.com/api/healthz
```

只运行 `--package-only` 不会连接服务器、发布或恢复订单。

## 发布行为

- 使用 Git 文件清单打包当前磁盘上的源码，包含未提交修改及未被忽略的新源码文件。采用源码目录和扩展名白名单；数据库、译文缓存、上传书籍、成品、日志、`.env`、密钥、虚拟环境及其他运行数据不进入发布包。
- 包内记录每个文件的 SHA-256。服务器维护前校验路径和校验值，备份即将覆盖的代码及当前依赖版本，然后覆盖源码、更新依赖。
- 已有 SQLite 服务升级会在服务器本地一致性备份订单数据库、译文缓存及 `.env`，权限为目录 0700、数据文件 0600，不导出到本机。其他数据库须先完成外部备份，脚本会拒绝自动继续。
- 保留密钥、定价及用户权限，只将翻译默认模型设为 Flash、关闭首轮复杂块直接升级，并把可见性超时设为至少 10800 秒且大于任务硬时限。
- 若数据库有排队或运行中的任务，拒绝部署。实际发布时短暂停止 API 和 beat，再次检查任务，避免检查后新任务进入；因此会有短暂维护中断。
- 重启 API、worker、beat，并检查服务状态及本机 `/healthz`；本机入口还会验证公网 `/api/healthz`（校验 JSON 状态为 `ok`）。任何失败返回非零退出码，不会输出部署成功。
- 正式发布持有服务器工程目录下的 `.deploy.lock` 排他锁，覆盖备份、配置更新和服务重启。另一台 Mac 同时发布会明确拒绝，不互相覆盖；`--check` 只是只读快照，不保留发布权。入口证书配置在维护前校验。
- 不清空缓存，不更改订单支付状态，不自动重跑历史订单。删除源码文件的迁移需单独处理；本脚本不会删除服务器上未列入发布包的文件。

整书时限默认 7200/7500 秒。若需覆盖，在服务器 `backend/.env` 设置 `EPUB_BOOK_SOFT_TIME_LIMIT` 和 `EPUB_BOOK_TIME_LIMIT`，硬时限必须大于软时限。

默认模型名为 `deepseek-flash`（DeepSeek V4.1 Flash）。旧 `deepseek-v4-flash` 和 `deepseek-v4-flash-vision-exp` 只保留兼容，历史订单和缓存键不批量改写。最新官方说明以[更新日志](https://api-docs.deepseek.com/zh-cn/updates/)为准，Pro 显式选择仍保留。

## 两台 Mac 共用工程

每台 Mac 独立安装部署公钥：在各自工程目录运行 `bash scripts/setup-deploy-ssh.sh`，各自私钥保留在 `~/.ssh/id_ed25519_fixepub`。不通过 Git、共享文件夹或发布包同步私钥，不覆盖另一台 Mac 的公钥。已有受控密钥可在本机通过 `DEPLOY_KEY` 指定；不要把个人绝对路径写入仓库。

常规流程：工作区干净后运行 `git pull --ff-only`，确认两台 Mac 使用同一提交，再运行 `bash deploy.sh --check` 和 `bash deploy.sh`。有未提交修改时先保存并审查，不强制拉取或覆盖。负责修改的一台先提交和推送，另一台再拉取；`push.sh` 仍要求已提交的干净 `main`。

两台 Mac 只同步代码。生产 `.env`、订单数据库、译文缓存、上传文件和译后文件均留在服务器，部署不会用本机数据替换它们。不要同时发布；服务器锁是误操作兜底。这里只验证了隔离本机目录/密钥的离线测试，另一台 Mac 的真实免密连接仍需在那台机器执行 `--check`。

腾讯 TokenHub 备用通道已移除，旧的 `TOKENHUB_BASE_URL` 和 `TOKENHUB_API_KEY` 不再读取，服务器 `.env` 中残留这两项也不会启用该通道。翻译继续使用 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`；若曾手动把 TokenHub 地址填入 `OPENAI_BASE_URL` 或 `OPENAI_BASE_URL_FALLBACKS`，需将其改为仍可用的服务地址并配置对应密钥。发布包不包含任何 API 密钥。

2026-09-20 非 AI 格式转换默认价降为 **0.99 元/本**，覆盖繁简转换、竖排改横排和阅读器适配，批量转换按本数相乘；AI 精校仍按正文有效字数以 **3.99 元起**另行加价。独立 EPUB 格式修复默认价为 **0.99 元/本**。标准 Flash AI 翻译为 **3.99 元起**的字符阶梯价：前 30 万字符 ¥0.05/千字符、30-100 万字符超出部分 ¥0.035/千字符、100 万字符以上超出部分 ¥0.025/千字符；高质量、文学和 Pro 分别使用 1.5、2 和 3.4 倍率。部署在完成 `.env`/数据库备份后，只迁移未设置/空值或精确匹配旧默认值的价格配置，保留其他显式自定义价和 `TRANSLATION_PRICE_CNY` 固定价覆盖。已有转换、翻译、批量与修复订单继续按下单时持久化的 `expected_amount` 或保存的冻结报价验款，均不重写历史金额。

独立修复的新订单将报价、支付金额和状态原子保存到 `REPAIR_UPLOAD_DIR/<job_id>/order.json`（0600），进程重启可加载，重复发起支付及回调按原金额校验。已确认付款但未结束的本地修复在下次显式支付恢复（`POST /recover`，刷新支付返回页会调用）时重新调度，同一 API 进程避免重复线程；修复成品写完后原子替换，避免中断留下半个 EPUB。默认目录仍是 `/tmp/epub-repair`，与原文件生命周期一致；如需跨系统清理/重启长期保留，请在生产配置持久目录。更新前只有内存状态、且从未写入元数据的旧独立修复订单，不能凭文件猜测支付状态恢复；这与数据库中的转换订单不同。

对于已确认不是测试单、旧运行价格确为 5.99 元的单笔在内存中的待付款修复订单，可使用上面的专用发布选项。它在同一部署锁内校验发布包，暂时停止 Nginx 并保持旧 API 运行；`migrate-legacy-repair.py` 核验固定的已审旧版源码哈希、单进程和本地监听、进程与文件配置、其他活跃旧任务、源文件和最终只读状态后，排他写入 0600 的 `order.json`。它只延续服务器的 `pending_payment` 状态、原价和原商户订单号，不声称支付宝尚未付款，也不调用支付或模型。源文件保持不变，迁移证据仅保存到服务器私有备份目录。

此专用选项同时声明操作者已核实启动后未修改旧修复价格；邮件配置等与价格无关的更新可以晚于进程启动。未知金额、测试单、其他旧版本、多进程、其他活跃旧内存任务、正在处理、已有元数据或不明确的文件均需单独处理，脚本会拒绝迁移。新的状态接口返回冻结金额，旧任务页面刷新后仍显示原价，新任务使用 0.99 元。

专用发布完成后，只有同一旧任务仍可通过 API 读取时才恢复 Nginx。若旧 API 已停止且新 API 无法读取迁移任务，会保留维护状态，不能无条件恢复旧版代码后开放入口：旧版代码本身不读取 `order.json`。应保留源文件、元数据和私有快照，先恢复支持持久化读取的 API。工具默认预览不写文件；传入 `--apply` 才写入，不覆盖已有元数据。相关离线检查为 `scripts/test_migrate_legacy_repair.py` 和 `scripts/test_deploy_legacy_gate.py`。

## 可选邮件通知

配置方法见 [EMAIL-NOTIFICATIONS.md](EMAIL-NOTIFICATIONS.md)。在目标服务器的工程目录执行 `python3 scripts/configure-email.py --qq --enable`，隐藏输入 `249998620@qq.com` 的 SMTP 授权码。脚本默认只保存目标机器的配置；本机配置不随部署包同步到服务器。`.env` 和授权码始终排除在 Git 与发布包之外。

新表 `job_email_subscriptions` 和 `payment_email_outbox` 随应用数据库初始化创建，不改写历史订单。顾客结果通知和商户收款通知使用两个独立派发线程，未配置 SMTP 时不启动。`OWNER_PAYMENT_EMAIL_TO` 默认 `249998620@qq.com`，`OWNER_PAYMENT_EMAIL_ENABLED` 单独控制商户通知；经服务端验证的新成交入队后立即唤醒，不等待书籍完成。完成代码部署、目标服务器配置和真实收件验收后，才可确认线上邮件功能可用。

## 失败排查及代码回退

### 已完成订单的定点修正版

代码部署不自动替换历史订单成品。需要另行获得该书修订/保留术语确认后，使用 `scripts/publish-reviewed-book.py`，先不带 `--apply` 检查，检查成功后才加该参数发布。
必须提供 `--database`、`--job-id`、`--candidate`、`--source-sha256`、`--previous-sha256`、`--candidate-sha256` 和 `--epubcheck-jar`；`--preserve-term` 仅用于用户已确认的确切保留词，可重复。
在服务器虚拟环境运行，使用实际项目目录，不把书稿、私钥或运行数据库放入 Git/源码发布包。

此工具核对三份文件版本、全书定位、正文/目录最终 QA、聚合交付判定与 EPUBCheck；持有同一服务器发布锁，任务运行中拒绝继续。
服务器本地保存数据库、旧 EPUB、完整复核报告，再通过事务切换单个订单的输出路径；不覆盖旧成品、不更改支付/权限/历史调用计数或缓存。原订单的下载/预览接口读取持久路径，用户无需重新付款或提交翻译。
备份报告含书稿及订单信息，只保留在受限服务器目录。若需回退，只回退经过确认的单个订单指针，不用整库备份覆盖其他用户的新订单。

备份位于服务器工程的 `deploy-backups/<时间戳>/`：

- `previous-code.zip`：发布前被覆盖文件；
- `new-files.json`：本次新增文件列表；
- `pip-freeze.txt`：更新依赖前的版本；
- `deploy-manifest.json`：本次发布的文件及哈希。

失败时先查看 `journalctl -u epub-factory -u epub-factory-worker -u epub-factory-beat -n 100 --no-pager`。发布失败会尝试启动三个服务；若新代码或依赖无法运行，应回退后再重启，不能把启动尝试当作恢复成功。

回退时停止三个服务，把 `previous-code.zip` 解压回工程目录，依据 `new-files.json` 移除本次新增的代码文件；若依赖已更新，按 `pip-freeze.txt` 恢复所需版本，再重启并检查 `/healthz`。`jobs.sqlite3`、`translation-cache.sqlite3` 和 `production.env` 是维护前的运行数据备份。代码回退不自动恢复数据库；如需恢复，须停止全部服务并另行核验，避免覆盖发布后新订单。脚本不会自动执行数据库回滚。
