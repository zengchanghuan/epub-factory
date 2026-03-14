# EPUB Factory

一个可产品化演进的 EPUB 转换引擎：支持竖排转横排、繁简互转、AI 全书翻译、双语对照输出，以及 Kindle/Apple Books 设备特化编译。生产主站域名：**fixepub.com**（腾讯云）。

## 功能一览

### 引擎（ExtremeCompiler Pipeline）

| 模块 | 功能 |
|---|---|
| `CjkNormalizer` | 竖排 → 横排 CSS 清洗，繁体 → 简体（OpenCC，可选台湾/香港变体）；解码层支持编码探测+回退（UTF-8/Big5/GBK 等） |
| `CssSanitizer` | 移除硬编码字体、行高、背景色 |
| `TypographyEnhancer` | 注入 orphans/widows，修复省略号和破折号 |
| `StemGuard` | 表格防溢出，MathML/SVG 公式保护 |
| `DeviceProfileCompiler` | Kindle 墨水屏去色 / Apple Books WebKit 前缀 |
| `SemanticsTranslator` | 异步 LLM 全书翻译（SQLite 缓存 + 术语表 RAG） |
| `TocRebuilder` | 启发式重建 TOC 目录 + 锚点注入 |
| `EpubPackager` | 重打包 + SVG 大小写修复 + OPF 修复 |

### 可靠性

- **两级降级策略**：Full Pipeline 失败 → Safe Mode（仅转换方向）
- **清洗器异常隔离**：单个 Cleaner 失败不影响整体任务
- **Pipeline 阶段耗时埋点**：每次转换输出详细耗时摘要

### AI 翻译

- 异步并发（`asyncio` + `Semaphore`），`tenacity` 指数退避重试
- SQLite 缓存去重（断点续传，零重复 Token 消耗）
- **术语表注入（RAG）**：传入 `{"原文术语": "目标术语"}` 强制统一翻译
- **双语对照模式**：原文 + 译文并排，含 `epub-original`/`epub-translated` class

### 存储

- **默认**：内存 `JobStore`（零依赖，重启后任务列表清空）
- **持久化任务列表**：在 `backend/.env` 中设置 `DATABASE_URL` 或 `EPUB_PERSISTENT_STORE=1`，自动切换为 SQLAlchemy 持久化（SQLite / PostgreSQL），任务中心与任务状态重启后保留。
  - 本地示例：`DATABASE_URL=sqlite:///./epub_jobs.db`
  - 生产示例：`DATABASE_URL=postgresql://user:password@host:5432/epub_factory`

## 目录结构

```
epub-factory/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 路由层
│   │   ├── converter.py         # EpubConverter 入口
│   │   ├── models.py            # Job / OutputMode / DeviceProfile
│   │   ├── storage.py           # 自动切换内存/持久化存储
│   │   ├── storage_db.py        # SQLAlchemy 持久化实现
│   │   └── engine/
│   │       ├── compiler.py      # ExtremeCompiler（Pipeline 调度）
│   │       ├── unpacker.py
│   │       ├── packager.py
│   │       ├── toc_rebuilder.py
│   │       ├── translation_cache.py
│   │       └── cleaners/        # 各清洗器模块
│   ├── test_c1_*.py             # 测试套件（C1-C6）
│   └── requirements.txt
├── frontend/
│   ├── index.html               # 单页应用
│   ├── lib.js                   # 纯逻辑函数（可单元测试）
│   └── tests/                   # Node.js 前端测试（F1-F6）
└── docs/                        # 设计文档
    ├── PRODUCT-STRATEGY.md
    ├── ENGINE-DESIGN.md
    └── AI-TRANSLATION-DESIGN.md
```

## 快速启动

### 1) 启动后端 API

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

可选环境变量（`.env` 文件）：

```env
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
DATABASE_URL=postgresql://user:pass@localhost/epub_factory  # 留空使用内存存储
REDIS_URL=redis://127.0.0.1:6379/0
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
```

当设置 `REDIS_URL` 或 `CELERY_BROKER_URL` 时，新建任务会入队到 Celery，由 Worker 执行整本转换（任务名 `jobs.run_conversion`）。使用 Celery 时请同时配置 `DATABASE_URL`，否则 Worker 无法通过 `job_id` 加载任务。

**生产部署（如 AWS）**：见 [docs/DEPLOY-AWS.md](docs/DEPLOY-AWS.md)，含 RDS 持久化、ElastiCache、密钥与安全组建议。

### 1.1) 启动后台任务 Worker（Phase 1 基础设施）

```bash
cd backend
.venv/bin/celery -A app.infra.celery_app.celery_app worker --loglevel=info
```

最小健康任务名：

```text
infra.health.ping
```

可用以下方式快速验证 Celery 基础设施：

```bash
cd backend
.venv/bin/python test_d1_celery_bootstrap.py
```

### 2) 启动前端页面

```bash
cd frontend
python3 -m http.server 5173
```

浏览器打开 `http://127.0.0.1:5173`。页内提供「上传转换」与「任务中心」：上传后任务进入后台队列，可关闭页面稍后在任务中心查看结果；支持通过 `?job_id=xxx` 直链打开指定任务详情。

## API 概览

| 接口 | 说明 |
|---|---|
| `GET /healthz` | 健康检查 |
| `POST /api/v1/jobs` | 创建转换任务（multipart/form-data） |
| `GET /api/v1/jobs/{job_id}` | 查询任务状态 |
| `GET /api/v1/jobs/{job_id}/download` | 下载转换结果 |

### POST /api/v1/jobs 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `file` | File | — | .epub 或 .pdf 文件 |
| `output_mode` | string | `simplified` | `simplified` \| `traditional` |
| `device` | string | `generic` | `generic` \| `kindle` \| `apple` |
| `enable_translation` | bool | `false` | 是否开启 AI 翻译 |
| `target_lang` | string | `zh-CN` | 翻译目标语言 |
| `bilingual` | bool | `false` | 双语对照模式 |
| `glossary_json` | string | `null` | 术语表 JSON，如 `{"Harry": "哈利"}` |

## 运行测试

```bash
cd backend

# 一键回归（D1–D13 + C1–C6，推荐）
.venv/bin/python run_regression.py

# 或按文件单独运行
.venv/bin/python test_d1_celery_bootstrap.py
.venv/bin/python test_d2_task_data_models.py
.venv/bin/python test_d3_api_v2_skeleton.py
.venv/bin/python test_d4_celery_job_pipeline.py
.venv/bin/python test_d5_stage_events.py
.venv/bin/python test_d6_manifest.py
.venv/bin/python test_d7_translate_chapter.py
.venv/bin/python test_d8_reduce.py
.venv/bin/python test_d9_book_reduce.py
.venv/bin/python test_d10_status_resolver.py
.venv/bin/python test_d11_notifications.py
.venv/bin/python test_d12_translation_enhancement.py
.venv/bin/python test_d13_regression.py
.venv/bin/python test_c1_typography_and_fallback.py
.venv/bin/python test_c2_pipeline_metrics.py
.venv/bin/python test_c3_stem_guard.py
.venv/bin/python test_c4_bilingual.py
.venv/bin/python test_c5_persistent_store.py
.venv/bin/python test_c6_glossary_rag.py

# 前端测试（F1-F6，共 53 个用例，零依赖）
cd frontend/tests
node runner.js test_f1_bilingual.js
node runner.js test_f2_metrics.js
node runner.js test_f3_cost.js
node runner.js test_f4_f5_safemode_errorcode.js
node runner.js test_f6_history.js
```

## 待实现（Roadmap）

- [ ] 幽灵目录 AI 语义提取（LLM 推断无标签章节）
- [ ] AI 生成图片 Alt 文本（ADA/A11y 合规）
- [ ] Redis + Celery 任务队列（支持并发、重试）
- [ ] 用户鉴权（Supabase Auth）
- [ ] 配额控制 + Paddle 计费
- [ ] 域名上线 + SEO 内容发布
- [ ] ProductHunt 发布
