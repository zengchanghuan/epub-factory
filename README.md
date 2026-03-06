# EPUB Factory

一个可产品化演进的转换 MVP：提供后端 API 与网页前端，支持把竖排 EPUB 或 PDF 转成横排 EPUB（繁体或简体）。

## 目录结构

- `backend/`：FastAPI 后端（任务 API + 转换引擎）
- `frontend/`：网页控制台（拖拽上传、查询状态、下载结果）

## 工程边界（先保证可靠性）

- 失败处理：每个任务独立状态（`pending/running/success/failed`），异常不会中断全局服务。
- 可观测性：每个任务都生成 `trace_id`，后端输出结构化日志。
- 可替换性：转换逻辑已独立在 `backend/app/converter.py`，后续可替换为 C# 或 Go 引擎。

## 快速启动

### 1) 启动后端 API

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### 2) 启动前端页面

新开终端：

```bash
cd frontend
python3 -m http.server 5173
```

浏览器打开 `http://127.0.0.1:5173`

## API 概览

- `GET /healthz`：健康检查
- `POST /api/v1/jobs`：创建转换任务（`multipart/form-data`）
  - `file`：EPUB 或 PDF 文件
  - `output_mode`：`traditional` 或 `simplified`
- `GET /api/v1/jobs/{job_id}`：查询任务状态
- `GET /api/v1/jobs/{job_id}/download`：下载转换结果

## 后续 TODO（按你要求暂不实现）

- [ ] 鉴权（登录、Token、权限模型）
- [ ] PostgreSQL 持久化（任务、审计、用户）
- [ ] Redis 队列（异步消费 + 任务重试）
- [ ] 任务重试策略（指数退避、最大重试次数）
- [ ] Nginx 部署（静态前端 + API 反向代理）
- [ ] Docker Compose 一键启动（frontend + backend + postgres + redis + nginx）

