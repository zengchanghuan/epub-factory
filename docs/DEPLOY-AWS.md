# AWS 部署指南

本文档说明如何将 EPUB Factory 后端与前端部署到 AWS，便于在公网提供转换与翻译服务。**所有密钥与敏感信息均通过环境变量注入，不写入代码或文档。**

**生产主站**：域名 **fixepub.com**（在腾讯云购买），作为对外主站。部署到 AWS 后，在腾讯云 DNS 将 fixepub.com / www.fixepub.com 解析到 AWS 服务器公网 IP 或 ALB 即可。

**一步步操作**：若需要从零开始的顺序指引，见 [一步步部署（fixepub.com 上 AWS）](DEPLOY-STEP-BY-STEP.md)。

---

## 0. 快速开始（最小部署）

若你只想先把后端跑上 AWS，按下面顺序即可：

1. **开一台 EC2**：区域任选，Amazon Linux 2 或 Ubuntu 22.04，实例类型 `t3.small`（2 vCPU、2 GiB）起步；存储 20 GiB 足够。
2. **安全组**：入站放行 **22（SSH）**、**80（HTTP）**、**443（HTTPS）**；不要对公网开放 8000。
3. **SSH 登录**后安装 Python 3.10+、git，拉取本仓库，在 `backend/` 下执行：
   ```bash
   sudo yum install -y python3.11 python3.11-pip git   # Amazon Linux 2
   # 或 Ubuntu: sudo apt update && sudo apt install -y python3.11 python3.11-venv git
   git clone <你的仓库地址> epub-factory && cd epub-factory/backend
   python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
4. **配置环境变量**：在 `backend/` 下创建 `.env`（勿提交），至少写入：
   ```bash
   OPENAI_API_KEY=sk-xxx
   OPENAI_BASE_URL=https://api.deepseek.com/v1
   OPENAI_MODEL=deepseek-chat
   DATABASE_URL=sqlite:///./epub_jobs.db
   ```
   生产建议用 RDS + PostgreSQL，见第 4 节。
5. **启动服务**（前台测试）：
   ```bash
   .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   前端由同一进程托管，无需单独起前端。
6. **用 Nginx 反代**：在 EC2 上安装 Nginx，将 80/443 反代到 `127.0.0.1:8000`，并配置 SSL（如 Let’s Encrypt）。这样公网只访问 80/443，不暴露 8000。
7. **进程守护**：用 systemd 管理 uvicorn，重启后自动拉起，见第 3 节末尾。

完成以上步骤后，通过 `http://你的EC2公网IP` 或绑定域名（如 fixepub.com）即可访问；任务数据落在本机 SQLite，如需多机或更稳可改用 RDS。若使用 fixepub.com，请在腾讯云 DNS 添加 A 记录指向 EC2 公网 IP（或 CNAME 指向 ALB）。

---

## 1. 架构建议

| 组件 | 推荐方案 | 说明 |
|------|----------|------|
| 计算 | EC2 或 ECS Fargate | 单机可先选 EC2；多实例/自动扩缩用 ECS |
| 任务列表与元数据 | RDS PostgreSQL 或 EC2 上 SQLite | 持久化任务中心、阶段、通知；`DATABASE_URL` 指向 RDS 或本地 SQLite 路径 |
| 后台任务队列（可选） | ElastiCache Redis | 与 Celery 配合做异步转换；不配置则用 FastAPI BackgroundTasks 进程内执行 |
| 文件存储（可选） | S3 + 本地缓存 | 上传/输出可放 S3，减少单机磁盘占用；当前版本默认 `backend/uploads` 与 `backend/outputs` 本地目录 |
| 密钥与配置 | AWS Systems Manager Parameter Store 或 Secrets Manager | 将 `OPENAI_API_KEY`、`DATABASE_URL`、`SMTP_*` 等写入 SSM/Secrets，启动时注入环境变量 |

---

## 2. 环境变量清单（名称仅作参考，值从 SSM/Secrets 或 .env 注入）

**必配（运行与翻译）：**

- `OPENAI_API_KEY` — 翻译 API 密钥，勿提交到仓库
- `OPENAI_BASE_URL` — 如 `https://api.deepseek.com/v1`
- `OPENAI_MODEL` — 如 `deepseek-chat`

**持久化任务列表：**

- `DATABASE_URL` — 例如 `postgresql://user:password@your-rds.region.rds.amazonaws.com:5432/epub_factory`，或 SQLite：`sqlite:///./epub_jobs.db`

**后台任务（可选）：**

- `REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` — 若使用 ElastiCache：`redis://your-elasticache.region.cache.amazonaws.com:6379/0`

**通知（可选）：**

- `NOTIFY_EMAIL_ENABLED`、`NOTIFY_EMAIL_TO`、`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`

**其他：**

- `SKIP_PAYMENT_CHECK` — 生产环境勿设为 true，或不要设置
- `ADMIN_SECRET` — 可选；设置后访问 `/api/v2/admin/translation-stats`、`/api/v2/admin/error-stats` 需在请求头带 `X-Admin-Key: <ADMIN_SECRET>`
- `SENTRY_DSN` — 可选；设置后任务失败会自动上报到 Sentry

---

## 3. EC2 单机部署（最小可用）

1. **启动 EC2**：Amazon Linux 2 或 Ubuntu，建议 2 vCPU、4 GiB 起（翻译时内存占用较高）。
2. **安装依赖**：Python 3.10+、pip、venv；若用 Celery 再装 Redis 客户端。
3. **拉取代码**，在 `backend/` 下执行：
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. **配置环境变量**：在 `backend/.env` 中写入上述变量，或从 SSM Parameter Store 拉取后 `export` 再启动。**不要将 .env 提交到 Git。**
5. **持久化任务列表**：设置 `DATABASE_URL`。若用 SQLite，指向应用目录下路径（如 `sqlite:///./epub_jobs.db`），并保证该目录有写权限且备份；若用 RDS，创建数据库后填入 `DATABASE_URL`。
6. **启动服务**：
   ```bash
   cd backend
   .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   前端由同一进程托管（根路径静态文件），无需单独起前端服务。
7. **安全组**：仅开放 80/443（经 Nginx 或 ALB 反向代理）及 SSH；不要对公网直接开放 8000（或仅临时调试）。
8. **进程守护**：用 systemd 管理 uvicorn，保证重启后自动拉起。示例 unit 文件 `/etc/systemd/system/epub-factory.service`：
   ```ini
   [Unit]
   Description=EPUB Factory API
   After=network.target

   [Service]
   Type=simple
   User=ec2-user
   WorkingDirectory=/home/ec2-user/epub-factory/backend
   EnvironmentFile=/home/ec2-user/epub-factory/backend/.env
   ExecStart=/home/ec2-user/epub-factory/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   然后执行：`sudo systemctl daemon-reload && sudo systemctl enable epub-factory && sudo systemctl start epub-factory`。路径按你实际部署目录修改。

---

## 4. 使用 RDS PostgreSQL 做任务持久化

1. 在 RDS 创建 PostgreSQL 实例，记下 endpoint、端口、数据库名、用户名与密码。
2. 在应用所在 EC2/ECS 的环境变量中设置：
   ```bash
   DATABASE_URL=postgresql://用户名:密码@endpoint:5432/数据库名
   ```
3. 首次启动带 `storage_db` 的后端时，会自动创建所需表（见 `storage_db.py` 中的模型与迁移逻辑）。
4. 重启后端或扩容多实例后，任务中心数据均来自 RDS，任务列表持久化。

---

## 5. 使用 Redis + Celery（可选）

- 在 ElastiCache 创建 Redis 集群，获取 endpoint。
- 设置 `CELERY_BROKER_URL`、`CELERY_RESULT_BACKEND`、`REDIS_URL` 为 `redis://...`。
- 启动 Celery Worker（与 uvicorn 同机或另起 EC2）：
  ```bash
  cd backend && .venv/bin/celery -A app.infra.celery_app worker --loglevel=info
  ```
- 使用 Celery 时务必同时配置 `DATABASE_URL`，否则 Worker 无法按 `job_id` 加载任务。

---

## 6. 安全与密钥

- **OPENAI_API_KEY**、**DATABASE_URL** 中的密码、**SMTP_PASSWORD** 等一律通过环境变量或 AWS SSM/Secrets Manager 注入，不写入代码、不提交到 Git。
- `.env` 已在 `.gitignore` 中，部署时在服务器上单独创建并限制权限（如 `chmod 600 backend/.env`）。
- 生产环境关闭 `SKIP_PAYMENT_CHECK`，确保翻译走支付校验。

---

## 7. 简要检查清单

- [ ] 环境变量已配置（含 `DATABASE_URL` 以持久化任务列表）
- [ ] `backend/.env` 未提交、权限收紧
- [ ] 安全组仅开放必要端口，8000 不直接对公网
- [ ] 若用 RDS，应用与 RDS 同 VPC 或通过 VPC 访问
- [ ] 若用 Celery，Redis 与 Worker 可访问且 `DATABASE_URL` 已设
