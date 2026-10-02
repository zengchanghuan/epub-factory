---
title: EPUB Factory
---

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

- 自适应异步并发与动态 JSON 批量：稳定时提高吞吐，错误升高时自动降并发、拆小批次
- SQLite 版本化缓存去重：标准模式兼容复用，高质量/文学模式仅使用同配置已验证缓存
- **术语表注入（RAG）**：传入 `{"原文术语": "目标术语"}` 强制统一翻译
- **三档质量模式**：标准、高质量选择性语义审校，以及带全书风格档案、章节润色和原文语义回查的文学模式
- **双语对照模式**：原文 + 译文并排，含 `epub-original`/`epub-translated` class
- **图片说明翻译**：翻译 XHTML 中的 `caption` / `figcaption` / `legend` 文本并纳入成品 QA；图片像素内部文字仍需 OCR（见 Roadmap）

当前生产代码架构与翻译质量门禁见 [docs/ARCHITECTURE-DIAGRAM.md](docs/ARCHITECTURE-DIAGRAM.md)。

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

**线上部署（腾讯云生产服务）**：统一使用项目根目录的 `deploy.sh`，说明见 [docs/DEPLOY.md](docs/DEPLOY.md)。

### 1.1) 启动分队列 Worker

三个独立终端中分别启动书籍 Worker、维护 Worker 和 beat；API 与 Worker 必须共用持久数据库、Redis 及文件目录。也可在工程根目录运行 `docker compose up --build`。

```bash
cd backend
.venv/bin/python -m app.infra.worker book
# 另一个终端（同样先 cd backend）：
.venv/bin/python -m app.infra.worker housekeeping
# 第三个终端：
.venv/bin/celery -A app.infra.celery_app:celery_app beat --loglevel=info
```

```mermaid
flowchart LR
    API[API / 书籍投递] --> BQ[celery 队列] --> BW[book Worker]
    Beat[beat / 维护任务] --> HQ[housekeeping 队列] --> HW[housekeeping Worker · 并发 1]
```

书籍保留原 `celery` 队列及原执行预算；支付对账、余额巡检、健康任务 `infra.health.ping` 使用 `housekeeping`。维护独立软/硬时限默认 1500/1800 秒，可用 `CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT` / `CELERY_HOUSEKEEPING_TIME_LIMIT` 配置。单个长对账仍会阻塞后续维护任务，队列隔离不是实时 SLA，但不占用书籍 Worker。

不要再运行不指定角色的裸 `celery ... worker`：同时订阅两个队列或维护并发不为 1 会在消费前被拒绝。兼容旧直接命令时也必须使用 prefork（可省略以使用默认池），禁用 autoscale 和排除队列参数；部署预检拒绝重复或含糊的角色相关选项。生产新增第四个 systemd 服务前，先按 [首次双 Worker 迁移](docs/DEPLOY.md#首次双-worker-迁移) 审查现有单元；本轮代码不自动修改或安装生产服务。

Celery 在构建配置前固定加载 `backend/.env`，已导出的 shell/systemd/容器变量优先，不读取其他当前目录的 `.env`。Worker 启动后也禁止通过远程 `add_consumer`、`cancel_consumer`、`pool_grow`、`pool_shrink`、`autoscale` 改变队列或并发；只读 `inspect`/`ping` 保留。需要调整角色/并发时，修改配置并在任务排空后按部署流程重启。

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
| `file` | File | — | .epub、.mobi、.azw3、.docx 或 .md 文件；暂不支持 PDF |
| `output_mode` | string | `simplified` | `simplified` \| `traditional` |
| `device` | string | `generic` | `generic` \| `kindle` \| `apple` |
| `enable_translation` | bool | `false` | 是否开启 AI 翻译 |
| `target_lang` | string | `zh-CN` | 翻译目标语言 |
| `bilingual` | bool | `false` | 双语对照模式 |
| `translation_quality` | string | `standard` | `standard`（Flash/0.3）、`high`（Pro/0.2/选择性语义审校）或 `literary`（Pro/0.2/风格档案+章节润色+语义回查） |
| `cache_policy` | string | 按档位 | `reuse`、`verified` 或 `fresh`；高质量和文学模式默认 `verified` |
| `translation_model` | string | 按档位 | `deepseek-v4-flash` 或 `deepseek-v4-pro` |
| `temperature` | float | 按档位 | 标准模式 `0.3`，高质量和文学模式 `0.2` |
| `glossary_json` | string | `null` | 术语表 JSON，如 `{"Harry": "哈利"}` |

## 运行测试

推荐从仓库根目录使用隔离门禁，避免旧测试导入应用时读取工作区 `.env` 或数据库。门禁只复制白名单源码，使用临时数据库/缓存与假密钥，阻止 Python 子进程联网；不会部署或安装依赖。

```bash
# evidence-dir 必须不存在，且位于仓库外；请换成本次唯一目录。
backend/.venv/bin/python scripts/release-gate.py \
  --python "$PWD/backend/.venv/bin/python" \
  --node node \
  --epubcheck-jar "$PWD/tools/epubcheck-5.1.0/epubcheck.jar" \
  --evidence-dir /private/tmp/fixepub-release-new-run \
  --profile offline
```

`offline` 运行完整后端 catalog 与前端单元测试，不代表真实书验收；`history` 额外要求三本历史书的原稿、旧成品和基线共 9 份文件全部匹配固定 SHA，并实际转换、检查和下载。缺文件必须失败，不能用跳过代替通过。

依赖要求、真实样本参数和双 Mac 交接见 [隔离回归与交接](docs/RELEASE-GATE-HANDOFF.md)。第二台 Mac 的真实运行验收按用户要求延期；本机通过不代表双机、生产支付或收费模型效果已验证。

## 待实现（Roadmap）

### 支付与合规 MVP (P0)
- [ ] **网站合规三件套** (提交 Lemon Squeezy 审核前必做)
  - [ ] Privacy Policy (隐私政策)：声明上传文件处理后删除不留底。
  - [ ] Terms of Service (服务条款)：说明预付费机制。
  - [ ] Refund Policy (退款政策)：明确“数字服务一经执行不退款”。
  - [ ] Contact Us：配置官网支持邮箱。
- [ ] **极简支付接入** (Lemon Squeezy)
  - [ ] 前端：上传后拉起 Checkout (传参 `task_id`)，先不写复杂的购物车。
  - [ ] 后端：编写单个 Webhook 接收 `order_created` 回调，更新状态并触发翻译任务。

### 体验优化 (P1)
- [ ] **多语言支持 (i18n)**：支持中英等界面语言切换，适配出海需求。
- [ ] **Dark / Light 模式**：前端适配暗黑模式，提升夜间使用体验。

### 核心功能 (P2)
- [ ] **PDF 翻译（方案待审核，入口仍关闭）**：文本／扫描／混合 PDF，MinerU 免费 API 与 DeepSeek Flash OCR 双通道，输出 EPUB＋原页码对照；详见 [PDF 翻译技术架构](docs/PDF-TRANSLATION-ARCHITECTURE.md)。
- [ ] 幽灵目录 AI 语义提取（LLM 推断无标签章节）
- [ ] AI 生成图片 Alt 文本（ADA/A11y 合规）
- [ ] 图片像素文字 OCR 翻译（可选 OCR、术语表复用、安全重绘/回退、独立 QA 与成本统计）
- [x] Redis + Celery 整本任务队列代码路径（未配置时回退 BackgroundTasks/本地线程；章节并发在 Worker 内执行）
- [ ] 用户鉴权（Supabase Auth）
- [ ] 域名上线 + SEO 内容发布
- [ ] ProductHunt 发布
