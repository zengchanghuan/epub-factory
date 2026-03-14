# 繁体→简体转换与横竖排方案

**版本**: 方案 v1（已实现）  
**状态**: 已实现：繁体来源参数、解码层编码探测+回退、文档与前端

---

## 一、需求摘要

| 需求 | 说明 |
|------|------|
| 1. 输出一律横排 | 输入为繁体横排或繁体竖排，转换到简体时**一律输出横排** |
| 2. 编码与地域变体 | 兼容不同繁体编码方式；可选区分台湾/香港繁体以提升转换质量 |
| 3. 是否用 LLM | 明确：繁→简**不需要 LLM**，用规则/词库方案即可 |

---

## 二、方案结论（先出结论）

- **布局**：当前管线已做到「竖排→横排 + 可选繁→简」，输出选简体时**一律横排**，无需改逻辑，仅需在文档与前端文案中写清。
- **繁→简实现**：**不使用 LLM**，沿用 **OpenCC**（规则+词库），已在 `CjkNormalizer` 中使用 `t2s`。
- **编码**：EPUB 标准为 UTF-8。解码层已实现**编码探测 + 回退**（`app.utils.encoding.decode_with_fallback`）：优先 UTF-8，失败时可选 chardet 探测，再按 Big5/GBK/GB18030/cp950 回退，保证不抛异常。
- **地域变体**：通过 **OpenCC 配置** 区分「台湾繁体」「香港繁体」「通用繁体」，对应 `tw2s` / `hk2s` / `t2s`，作为**可选参数**由前端传入，默认 `t2s`。

---

## 三、详细设计

### 3.1 横排/竖排与「一律横排」

- 当前行为（`CjkNormalizer`）：
  - **CSS**：`_horizontalize_css()` 将 `writing-mode: vertical-*`、`text-orientation: upright`、`direction: rtl` 等统一改为横排。
  - **标点**：`_replace_vertical_punctuation()` 将竖排用标点（如 U+FE10–FE16 等）替换为横排对应字符。
- 因此：
  - **繁体竖排** → 先被改为横排样式与标点，再若 `output_mode=simplified` 则做繁→简 → **横排简体**。
  - **繁体横排** → 无布局改动，仅若选简体则做繁→简 → **横排简体**。
- **结论**：逻辑上已满足「转换到简体一律横排」，无需新增分支；只需在产品/文档中说明「输出为简体时均为横排」。

### 3.2 繁→简：为什么不用 LLM

| 维度 | 说明 |
|------|------|
| 任务性质 | 繁↔简是**字形/词形映射**（同一语言的不同书写系统），不是跨语言翻译 |
| 标准做法 | 开源 **OpenCC** 已覆盖字符级、词级与地域习惯（台/港），维护成熟 |
| 成本与延迟 | 规则转换无 API 成本、无网络、延迟低，适合全书处理 |
| 可控性 | 规则可审计、可复现，无模型幻觉 |
| LLM 适用场景 | 若要做「台湾用语→大陆用语」等**语体/用词风格**转换，再考虑 LLM 作为可选后处理；与「繁→简」解耦 |

**结论**：繁→简统一用 **OpenCC**，不引入 LLM。

### 3.3 不同繁体「编码/变体」的适配

- **字符编码（已实现）**
  - EPUB 规范：XHTML/NCX 等为 **UTF-8**。解码统一走 `app.utils.encoding.decode_with_fallback(content)`：
    1. 先尝试严格 UTF-8 解码；
    2. 失败则用 **chardet**（可选依赖）探测编码并解码；
    3. 再按固定回退列表尝试：big5、gbk、gb18030、cp950；
    4. 最后保底 `utf-8` + `errors="replace"`，保证始终返回 str。
  - 依赖：`chardet` 已加入 `requirements.txt`，用于非 UTF-8 历史文件的自动识别。

- **地域变体（台湾 vs 香港 vs 通用）**
  - 用 **OpenCC 预设** 区分即可，无需 LLM：

    | 选项 | OpenCC 配置 | 说明 |
    |------|-------------|------|
    | 通用 / 自动 | `t2s` | 默认，适合混合或未知来源 |
    | 台湾繁体 | `tw2s` | 台湾正体→简体，用词更贴台湾习惯 |
    | 香港繁体 | `hk2s` | 香港繁体→简体 |

  - 实现：在 `CjkNormalizer` 增加参数 `traditional_variant: str = "auto"`（或 `"tw"` / `"hk"`），映射到上述配置；API/前端增加可选参数「繁体来源」，默认「自动」对应 `t2s`。

### 3.4 与现有管线衔接

- **入口**：用户选择「输出：简体」时，走现有 `output_mode=simplified` 分支；若增加「繁体来源」，则从 API 传到 `converter` → `compiler` → `CjkNormalizer`。
- **顺序**：保持现有顺序：**先横排化（CSS + 标点）→ 再繁→简（OpenCC）**。这样竖排标点先被替换为横排符号，再统一做繁简转换，避免竖排专用符残留。
- **不译场景**：若同时开启「AI 翻译」，翻译在 SemanticsTranslator 中处理**英文等外语→中文**；繁→简仍在 CjkNormalizer 中用 OpenCC 完成，两者串联、不冲突。

### 3.5 标点与 ROADMAP 的衔接

- `docs/ROADMAP-TYPOGRAPHY-SERVICE.md` 中「繁体竖排标点修正」（弯引号「」、旋转等）主要针对**保留竖排**的排版优化。
- 本方案是「**转成简体且一律横排**」：竖排标点已在 `_replace_vertical_punctuation` 中处理；若后续做「竖排保留」路线，再接入 ROADMAP 中的标点细则。

---

## 四、实现清单（按优先级）

| 序号 | 项 | 类型 | 说明 | 状态 |
|------|----|------|------|------|
| 1 | 文档/文案 | 必做 | 在说明中明确：输出为简体时一律横排；繁→简采用 OpenCC，不用 LLM | ✅ 已做 |
| 2 | 繁体来源可选参数 | 推荐 | API/前端增加「繁体来源」：自动(t2s) / 台湾(tw2s) / 香港(hk2s)；`CjkNormalizer` 接收并选用对应 OpenCC 配置 | ✅ 已做 |
| 3 | 编码探测与回退 | 已实现 | `app.utils.encoding.decode_with_fallback` + chardet，UTF-8 失败后探测并回退 Big5/GBK 等 | ✅ 已做 |

---

## 五、验收要点

- 繁体竖排 EPUB，选「横排简体」→ 输出为横排且为简体。
- 繁体横排 EPUB，选「横排简体」→ 输出仍为横排且为简体。
- 选「繁体来源：台湾」时，使用 `tw2s`；选「香港」时使用 `hk2s`；默认使用 `t2s`。
- 不调用 LLM 即可完成繁→简；AI 翻译仅用于外文→中文等语义翻译。

---

## 六、依赖与风险

- **依赖**：`opencc-python-reimplemented`（t2s/tw2s/hk2s）、`chardet`（编码探测）。
- **风险**：无；仅增加可选参数与解码回退，不改动现有「一律横排」行为。

---

## 七、实现位置速查

| 功能 | 位置 |
|------|------|
| 解码层编码探测+回退 | `backend/app/utils/encoding.py`：`decode_with_fallback()` |
| 繁→简 + 繁体来源 | `backend/app/engine/cleaners/cjk_normalizer.py`：`traditional_variant` → OpenCC 配置 |
| API 参数 | `main.py`：`traditional_variant: TraditionalVariant`（v1/v2 创建任务） |
| 前端选项 | `frontend/index.html`：输出模式为「横排简体」时显示「繁体来源」下拉（自动/台湾/香港） |
| 持久化 | `storage_db.py`：`JobRecord.traditional_variant` 及迁移 |
