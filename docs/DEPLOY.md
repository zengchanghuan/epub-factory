# 腾讯云部署

本工程的发布入口是根目录 `deploy.sh`。默认服务器为 `ubuntu@81.71.22.79:22`，工程目录 `/home/ubuntu/epub-factory`，网站 `https://fixepub.com`。不再依赖本机配置 `fixepub` SSH 别名或固定名称的私钥。

## 从本机部署

已提交的 `main` 分支可以一键推送并部署：`bash push.sh`。脚本要求工作区干净且 origin 推送地址为 `git@github.com:zengchanghuan/epub-factory.git`；Git 推送成功后才执行免密部署。部署失败时推送仍已完成，解决原因后运行 `bash deploy.sh` 即可。普通 `git push` 仍只推送代码；此入口不是 GitHub Actions 自动部署，也不会等待远端 CI。

```bash
# 首次安装部署公钥，需要在本机终端输入一次服务器密码。
bash scripts/setup-deploy-ssh.sh

# 后续免密部署，可在任意目录执行。
bash /Users/tristan/workspace/epub-factory/deploy.sh

# 仅检查连接、服务器环境和运行中的任务，不发布。
bash deploy.sh --check

# 使用已有 SSH 私钥。
DEPLOY_KEY=/绝对路径/fix_epub.pem bash deploy.sh

# 如需覆盖目标地址。
DEPLOY_HOST=ubuntu@81.71.22.79 DEPLOY_PORT=22 bash deploy.sh
```

本机需要 Python 3.9+、Git、OpenSSH 和 curl。SSH 首次连接时，请核对服务器主机指纹。服务器需要已有生产 `.env`、Python 虚拟环境、Java、EPUBCheck 5.1.0，以及三个 systemd 服务：`epub-factory`、`epub-factory-worker`、`epub-factory-beat`。这是已有服务的升级脚本，不负责首次建站或配置密钥。

免密安装脚本生成专用密钥 `~/.ssh/id_ed25519_fixepub`，只把公钥追加到服务器 `authorized_keys`，保留已有公钥；不保存密码、不修改 SSH 服务配置。私钥没有口令，保存在本机权限受限的 SSH 目录中，不进入工程或部署包。已有密钥可通过 `DEPLOY_KEY` 指定；带口令的密钥需先加入 ssh-agent。仅生成密钥可运行 `bash scripts/setup-deploy-ssh.sh --prepare`。部署入口启用 `BatchMode=yes` 和严格主机校验，认证异常会立即失败，不退回密码登录。

服务器执行 `sudo -n`：若账号没有免密 sudo，优先使用下面的腾讯云终端方式，在同一终端执行 `sudo -v` 后发布。脚本不会保存、传输文件形式的密码，也不会修改 sudo 权限。

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
- 包内记录每个文件的 SHA-256。服务器校验路径和校验值，备份即将覆盖的代码及当前依赖版本，然后覆盖源码、更新依赖。
- 若数据库有排队或运行中的任务，拒绝部署。实际发布时短暂停止 API 和 beat，再次检查任务，避免检查后新任务进入；因此会有短暂维护中断。
- 重启 API、worker、beat，并检查服务状态及本机 `/healthz`；本机入口还会验证公网 `/api/healthz`（校验 JSON 状态为 `ok`）。任何失败返回非零退出码，不会输出部署成功。
- 不清空缓存，不更改订单支付状态，不自动重跑历史订单。删除源码文件的迁移需单独处理；本脚本不会删除服务器上未列入发布包的文件。

整书时限默认 7200/7500 秒。若需覆盖，在服务器 `backend/.env` 设置 `EPUB_BOOK_SOFT_TIME_LIMIT` 和 `EPUB_BOOK_TIME_LIMIT`，硬时限必须大于软时限。

腾讯 TokenHub 备用通道已移除，旧的 `TOKENHUB_BASE_URL` 和 `TOKENHUB_API_KEY` 不再读取，服务器 `.env` 中残留这两项也不会启用该通道。翻译继续使用 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`；若曾手动把 TokenHub 地址填入 `OPENAI_BASE_URL` 或 `OPENAI_BASE_URL_FALLBACKS`，需将其改为仍可用的服务地址并配置对应密钥。发布包不包含任何 API 密钥。

## 失败排查及代码回退

备份位于服务器工程的 `deploy-backups/<时间戳>/`：

- `previous-code.zip`：发布前被覆盖文件；
- `new-files.json`：本次新增文件列表；
- `pip-freeze.txt`：更新依赖前的版本；
- `deploy-manifest.json`：本次发布的文件及哈希。

失败时先查看 `journalctl -u epub-factory -u epub-factory-worker -u epub-factory-beat -n 100 --no-pager`。发布失败会尝试启动 API/beat；若新代码或依赖无法运行，应回退后再重启，不能把启动尝试当作恢复成功。

回退时停止三个服务，把 `previous-code.zip` 解压回工程目录，依据 `new-files.json` 移除本次新增的代码文件；若依赖已更新，按 `pip-freeze.txt` 恢复所需版本，再重启并检查 `/healthz`。这份备份只覆盖代码和依赖清单，不是数据库备份，不能撤销应用启动时发生的数据迁移。脚本不会自动执行数据库回滚。
