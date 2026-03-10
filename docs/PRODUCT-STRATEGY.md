---
title: "EPUB Fixer 产品战略文档"
date: 2026-03-09
tags: ["EPUB", "产品定位", "SEO", "出海SaaS", "定价", "里程碑"]
status: "active"
merged_from:
  - "2026-03-03-EPUB竖排转横排产品化改造蓝图.md"
  - "2026-03-04-EPUB-Fixer-功能规划与系统设计.md"
  - "EPUB-Fixer-四维产品战略规划.md"
---

# EPUB Fixer 产品战略文档

---

## 一、产品定位

### 核心定位（一句话）

> **"The one-click EPUB cleaner for Kindle-perfect formatting — fix CSS mess, broken TOC, and CJK layout in seconds."**

### 目标用户

| 用户画像 | 核心痛点 | 付费意愿 |
|---|---|---|
| **自出版作者（Self-publishers）** | Word 导出的 EPUB 排版混乱，KDP/Apple 上架被拒 | ⭐⭐⭐⭐⭐ 极高，直接影响收入 |
| **轻小说/漫画爱好者** | 日文/中文竖排 EPUB 在 Kindle 上乱码 | ⭐⭐⭐⭐ 高，强烈需求 |
| **电子书收藏者/读者** | 从 Anna's Archive 下载的书排版很烂 | ⭐⭐⭐ 中，愿意付小额 |

### 差异化优势

| 对比维度 | 竞品（Calibre/Sigil/EPUBCheck） | EPUB Fixer |
|---|---|---|
| 使用门槛 | 需要安装、学习、手动操作 | **一键上传，自动处理** |
| CJK 支持 | 基本没有专项优化 | **竖排转横排、繁简互转** |
| 目标用户 | 技术用户 | **自出版作者 + 普通读者** |
| 输出标准 | 不保证 KDP 合规 | **100% 通过 EpubCheck** |

### 工程边界（先保证可靠性）

- 可靠性 > 性能 > 成本 > 效果增强
- 失败处理：每个任务独立状态（`pending/running/success/failed`），异常不中断全局服务
- 可观测性：每个任务生成 `trace_id`，后端输出结构化 JSON 日志
- 可替换性：转换引擎独立封装，可随时热替换

---

## 二、关键词策略（SEO）

### 第一层：精准高转化词（立即布局）

| 关键词 | 搜索意图 | 竞争度 | 优先级 |
|---|---|---|---|
| `epub fixer online` | 直接找工具 | 低 | P0 |
| `fix epub formatting` | 有明确问题 | 低 | P0 |
| `epub css cleaner` | 技术用户 | 极低 | P0 |
| `epub kindle formatter` | 自出版作者 | 低 | P0 |
| `clean epub css online` | 精准技术词 | 极低 | P0 |
| `fix epub for kindle` | 高意图 | 低 | P0 |
| `epub repair tool` | 文件损坏修复 | 中 | P1 |

### 第二层：CJK 特化词（核心护城河）

| 关键词 | 搜索意图 | 竞争度 |
|---|---|---|
| `epub vertical to horizontal` | 日漫/中文书转横排 | 极低 🏆 |
| `japanese epub kindle fix` | 日文轻小说阅读者 | 极低 🏆 |
| `chinese epub converter` | 中文书格式转换 | 低 🏆 |
| `epub simplified traditional chinese convert` | 繁简互转 | 极低 🏆 |
| `light novel epub fix kindle` | 轻小说社区 | 极低 🏆 |

> **战略重点**：CJK 相关词竞争度接近零，是最快能占领搜索排名的蓝海品类。

### 第三层：长尾内容词（博客 SEO 持续引流）

```
how to fix epub formatting for kindle
epub background color not changing in dark mode
epub table of contents broken fix
kindle epub css hardcoded font size fix
word to epub formatting issues fix
```

---

## 三、SEO 网站架构规划

```
epubfixer.com
│
├── /（首页）               → epub fixer, fix epub online
├── /fix-epub-css           → epub css cleaner
├── /kindle-formatter       → fix epub for kindle
├── /cjk-converter          → japanese epub fix, vertical to horizontal
├── /epub-toc-repair        → epub toc fix
├── /pricing                → 付费转化页
└── /blog/
    ├── how-to-fix-epub-formatting-for-kindle
    ├── epub-dark-mode-css-fix-guide
    ├── japanese-epub-kindle-vertical-horizontal-guide
    └── fix-epub-from-word-export
```

技术 SEO 要点：

- [ ] 每个工具页 `<h1>` 必须包含核心关键词
- [ ] 首页展示 Before/After 对比截图（影响 CTR）
- [ ] 页面加载速度 < 2 秒（Vercel + CDN）
- [ ] Schema Markup 标注为 SoftwareApplication

---

## 四、内容与冷启动策略

### 发布渠道

| 内容形式 | 发布渠道 | 目标 |
|---|---|---|
| "一键修复 EPUB CSS 排版" 工具帖 | `r/kindle` `r/Annas_Archive` `r/selfpublish` | 第一批种子用户 |
| Before/After 排版对比截图 | Reddit + Twitter/X | 视觉震撼，自然传播 |
| "How to fix EPUB for Kindle" 完整教程 | 博客（SEO 长尾词收录） | 持续自然搜索流量 |
| CJK 专项教程（日文轻小说修复指南） | `r/lightnovels` `r/manga` | 打入高黏性小众社区 |
| 工具发布帖 | ProductHunt | 获取初始评价和外链 |

### 站内转化内容

- **数字化成果展示**："已为您清除 2,415 行冗余 CSS，修复 12 个致命排版错误"
- **免费增值**：免费处理 1 本 → 付费解锁批量 / 高级功能
- **微反馈**：下载后提供 `👍 完美解决` / `👎 依然排版错乱` 两个按钮
- **失败用例反哺**：点 👎 后提示留下文件供工程师分析，持续加高护城河

---

## 五、定价策略

| 套餐 | 价格 | 内容 |
|---|---|---|
| Free | $0 | 每月 3 本，基础 CSS 清洗 |
| Pro | $9.9/月 | 无限量，全功能（CJK转换、TOC修复、设备编译） |
| Lifetime | $49 | 一次性买断，适合自出版作者 |

---

## 六、SaaS 数据模型参考（未来 PostgreSQL 迁移）

> 当前 MVP 使用内存 `JobStore`，以下为未来迁移 PostgreSQL 的参考设计。

### `profiles` 用户表

- `id`: uuid (PK, references auth.users)
- `email`: string
- `subscription_tier`: enum ('free', 'pro')
- `tokens_remaining`: int

### `epub_jobs` 任务表

- `id`: uuid (PK)
- `user_id`: uuid (FK)
- `status`: enum ('pending', 'processing', 'success', 'failed')
- `original_filename`: string
- `input_file_path` / `output_file_path`: string
- `options`: jsonb — `{"clean_css": true, "rebuild_toc": true, "device": "kindle"}`
- `error_message`: text
- `trace_id`: string
- `created_at` / `completed_at`: timestamp

### `job_events` 任务日志流水表（进度条展示）

- `job_id`, `step` (e.g., 'unzipping', 'cleaning_css', 'rebuilding_toc'), `status`, `created_at`

---

## 七、里程碑规划

```mermaid
graph LR
    A[确认域名 epubfixer.com] --> B[搭建落地页 Next.js + Vercel]
    B --> C[接入 Paddle 收款]
    B --> D[部署 Python 核心引擎 API]
    D --> E[上线基础功能: CSS清洗 + CJK转换]
    E --> F[Reddit 冷启动]
    F --> G[收集用户反馈 / 迭代]
    G --> H[发布 ProductHunt]
```

### Phase 1（✅ 已完成）：可用 MVP

- [x] 最小 API：创建任务、查询状态、下载结果
- [x] 完成"竖排繁体 -> 横排简体/繁体"核心路径
- [x] 上线最小 Web 控制台（上传 + 列表 + 下载）
- [x] 接入 AI 翻译（SemanticsTranslator + SQLite 缓存）
- [x] Kindle/Apple 设备特化编译
- [x] EpubCheck 闭环验证

### Phase 2（🔄 待执行）：稳定性增强

- [ ] 引入队列与 Worker 池（Redis + BullMQ 或 Celery）
- [ ] 增加结构化日志、TraceID、阶段耗时仪表盘
- [ ] 补齐失败重试与降级策略（主引擎失败时降级为仅修改方向）
- [ ] PostgreSQL 持久化（任务、审计）

### Phase 3（📋 规划中）：产品化闭环

- [ ] 鉴权（邮箱 Magic Link，Supabase Auth）
- [ ] 支持参数模板、批量任务
- [ ] 增加账单埋点与配额控制
- [ ] 域名上线 + SEO 内容发布
- [ ] ProductHunt 发布

---

## 八、验收标准（Definition of Done）

- [ ] 成功率：标准样本集转换成功率 >= 95%
- [ ] 稳定性：P1 故障支持 10 分钟内定位（凭 TraceID）
- [ ] 性能：单本中等体积 EPUB（5-20MB）P95 处理时长 < 60 秒
- [ ] 可维护性：新增一种规则不改动超过 2 个模块
- [ ] 可回滚性：任一版本升级失败可在 5 分钟内回退
