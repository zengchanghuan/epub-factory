/**
 * EPUB Factory — 前端纯逻辑库
 *
 * 所有函数均为纯函数（无副作用、无 DOM 依赖），
 * 可直接在 Node.js 中 require() 并做单元测试。
 */

// ─── 文件验证 ──────────────────────────────────────────────────────────────

/**
 * 检查文件是否为支持的类型
 * @param {string} filename
 * @returns {{ valid: boolean, error?: string }}
 */
function validateFile(filename) {
  const lower = (filename || "").toLowerCase();
  if (lower.endsWith(".epub") || lower.endsWith(".pdf")) {
    return { valid: true };
  }
  return { valid: false, error: "仅支持 .epub 或 .pdf 文件" };
}

// ─── 表单数据构建 ──────────────────────────────────────────────────────────

/**
 * 根据 UI 配置对象，返回需要发送给 API 的字段映射
 * @param {object} config
 * @param {string}  config.outputMode       - "simplified" | "traditional"
 * @param {string}  config.device           - "generic" | "kindle" | "apple"
 * @param {boolean} config.enableTranslation
 * @param {string}  config.targetLang       - e.g. "zh-CN"
 * @param {boolean} config.bilingual        - 双语对照模式
 * @returns {object} 字段名 → 值
 */
function buildFormFields(config) {
  const fields = {
    output_mode: config.outputMode || "simplified",
    device: config.device || "generic",
    enable_translation: String(Boolean(config.enableTranslation)),
  };

  if (config.enableTranslation) {
    fields.target_lang = config.targetLang || "zh-CN";
    fields.bilingual = String(Boolean(config.bilingual));
    if (config.glossaryJson) {
      fields.glossary_json = config.glossaryJson;
    }
  }

  return fields;
}

// ─── 状态文本映射 ──────────────────────────────────────────────────────────

const STATUS_TEXT = {
  pending:  "等待中",
  running:  "转换中...",
  success:  "完成",
  failed:   "失败",
};

/**
 * @param {string} status
 * @returns {string}
 */
function mapStatusText(status) {
  return STATUS_TEXT[status] || status;
}

/** API v2 状态 → 前端展示文案 */
const V2_STATUS_TEXT = {
  queued: "排队中",
  preprocessing: "预处理中",
  mapping: "映射中",
  translating: "翻译中",
  reducing: "汇总中",
  packaging: "打包中",
  validating: "校验中",
  completed: "完成",
  partial_completed: "部分完成",
  failed: "失败",
  cancelled: "已取消",
};

function mapV2StatusText(v2Status) {
  return V2_STATUS_TEXT[v2Status] || v2Status || "未知";
}

// ─── Job 元信息格式化 ──────────────────────────────────────────────────────

/**
 * 格式化设备显示文字
 * @param {string} device
 * @returns {string}
 */
function formatDevice(device) {
  const map = { generic: "通用", kindle: "Kindle", apple: "Apple Books" };
  return map[device] || device;
}

/**
 * 格式化 Job 元信息为展示用字符串
 * @param {object} job  - API 返回的 job 对象
 * @returns {string}
 */
function formatJobMeta(job) {
  const mode = job.output_mode === "simplified" ? "横排简体" : "横排繁体";
  const device = formatDevice(job.device);
  const parts = [`${mode} / ${device}`];
  if (job.enable_translation) {
    parts.push(`AI翻译(${job.target_lang})`);
    if (job.bilingual) parts.push("双语并排");
  }
  return parts.join(" · ");
}

// ─── Pipeline 耗时格式化 ───────────────────────────────────────────────────

/**
 * 将毫秒数格式化为可读字符串
 * @param {number} ms
 * @returns {string}
 */
function formatDuration(ms) {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * 解析 Pipeline 耗时摘要文本，返回阶段列表
 * 后端 summary 格式：每行 "  ✅/⚠️/❌ StageName   xxx.x ms"
 * @param {string} summaryText
 * @returns {Array<{name: string, ms: number, status: string}>}
 */
function parseMetricsSummary(summaryText) {
  const lines = summaryText.split("\n");
  const stages = [];
  // ⚠️ 是双码点 emoji（⚠ U+26A0 + ️ U+FE0F），需单独处理
  const re = /(✅|⚠️?|❌)\s+(.+?)\s+([\d.]+)\s+ms/u;
  for (const line of lines) {
    const m = re.exec(line);
    if (!m) continue;
    const icon = m[1].replace(/\uFE0F/g, ""); // 去掉 variation selector
    const statusMap = { "✅": "ok", "⚠": "warn", "❌": "error" };
    stages.push({
      name: m[2].trim(),
      ms: parseFloat(m[3]),
      status: statusMap[icon] || "ok",
    });
  }
  return stages;
}

/**
 * 从 summary 文本提取总耗时（ms）
 * @param {string} summaryText
 * @returns {number|null}
 */
function parseTotalMs(summaryText) {
  const m = /总耗时\s+([\d.]+)\s+ms/.exec(summaryText);
  return m ? parseFloat(m[1]) : null;
}

// ─── 翻译费用格式化 ────────────────────────────────────────────────────────

/**
 * 格式化 Token 消耗与费用
 * @param {{ totalTokens: number, costUsd: number }} stats
 * @returns {string}
 */
function formatTranslationCost(stats) {
  const tokens = stats.totalTokens?.toLocaleString() ?? "0";
  const cost = typeof stats.costUsd === "number"
    ? `$${stats.costUsd.toFixed(4)}`
    : "$0.0000";
  return `${tokens} tokens · ${cost} USD`;
}

// ─── SafeMode 检测 ─────────────────────────────────────────────────────────

/**
 * 从 Pipeline summary 判断是否使用了 Safe Mode
 * @param {string} summaryText
 * @returns {boolean}
 */
function isSafeMode(summaryText) {
  return summaryText.includes("safe") || summaryText.includes("SafeMode");
}

// ─── 错误码格式化 ──────────────────────────────────────────────────────────

const ERROR_CODE_HINTS = {
  CONVERT_FAILED: "引擎转换失败，可能是文件格式不兼容",
  TRANSLATION_FAILED: "AI 翻译未成功写入任何译文，请检查模型服务连接或稍后重试",
  PARTIAL_TRANSLATION: "部分段落翻译失败，结果可能不完整",
  EPUB_VALIDATION_FAILED: "EPUB 校验未通过，结果不可交付，请重试或联系支持",
  UPLOAD_TOO_LARGE: "文件超过大小限制",
  UNSUPPORTED_TYPE: "仅支持 .epub 或 .pdf 文件",
};

/**
 * @param {string|null} errorCode
 * @returns {string}
 */
function formatErrorCode(errorCode) {
  if (!errorCode) return "";
  const hint = ERROR_CODE_HINTS[errorCode];
  return hint ? `[${errorCode}] ${hint}` : `[${errorCode}]`;
}

// ─── 术语表解析 ────────────────────────────────────────────────────────────

/**
 * 把用户输入的术语表文本解析为 {原文: 译文} 对象
 *
 * 支持格式（每行一条）：
 *   Harry Potter = 哈利·波特
 *   Voldemort: 伏地魔
 *   空行、纯空白行自动跳过
 *
 * @param {string} text
 * @returns {{ glossary: object, errors: string[] }}
 */
function parseGlossaryInput(text) {
  const glossary = {};
  const errors = [];

  const lines = (text || "").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;  // 跳过空行

    // 支持 = 和 : 两种分隔符，取第一个出现的
    const eqIdx = line.indexOf("=");
    const colonIdx = line.indexOf(":");
    let sepIdx = -1;
    if (eqIdx >= 0 && colonIdx >= 0) sepIdx = Math.min(eqIdx, colonIdx);
    else if (eqIdx >= 0) sepIdx = eqIdx;
    else if (colonIdx >= 0) sepIdx = colonIdx;

    if (sepIdx < 0) {
      errors.push(`第 ${i + 1} 行格式错误（缺少 = 或 :）：${line}`);
      continue;
    }

    const src = line.slice(0, sepIdx).trim();
    const dst = line.slice(sepIdx + 1).trim();

    if (!src) { errors.push(`第 ${i + 1} 行原文为空`); continue; }
    if (!dst) { errors.push(`第 ${i + 1} 行译文为空`); continue; }

    glossary[src] = dst;
  }

  return { glossary, errors };
}

/**
 * 将 parseGlossaryInput 的结果序列化为 API 所需的 JSON 字符串
 * 若术语表为空返回 null
 * @param {string} text
 * @returns {string|null}
 */
function glossaryToJson(text) {
  const { glossary } = parseGlossaryInput(text);
  if (Object.keys(glossary).length === 0) return null;
  return JSON.stringify(glossary);
}

// ─── LocalStorage 历史记录 ────────────────────────────────────────────────

const HISTORY_KEY = "epub_factory_history";
const HISTORY_MAX = 5;

/**
 * 构造一条历史记录条目
 * @param {object} job     - API 返回的 job 对象
 * @param {string} apiBase - API base URL
 * @returns {object}
 */
function buildHistoryEntry(job, apiBase) {
  return {
    jobId: job.job_id,
    traceId: job.trace_id,
    filename: job.source_filename,
    meta: formatJobMeta(job),
    downloadUrl: job.download_url ? `${apiBase}${job.download_url}` : null,
    savedAt: new Date().toISOString(),
  };
}

/**
 * 加载历史记录列表（最多 HISTORY_MAX 条）
 * @param {object} storage - localStorage-like 对象
 * @returns {Array}
 */
function loadHistory(storage) {
  try {
    return JSON.parse(storage.getItem(HISTORY_KEY) || "[]");
  } catch {
    return [];
  }
}

/**
 * 保存一条新记录（去重 + 限制最大条数）
 * @param {object} storage
 * @param {object} entry  - buildHistoryEntry 的返回值
 * @returns {Array}       - 更新后的历史列表
 */
function saveHistory(storage, entry) {
  let list = loadHistory(storage);
  // 去重：已有相同 jobId 时覆盖
  list = list.filter(e => e.jobId !== entry.jobId);
  list.unshift(entry);
  if (list.length > HISTORY_MAX) list = list.slice(0, HISTORY_MAX);
  storage.setItem(HISTORY_KEY, JSON.stringify(list));
  return list;
}

/**
 * 清除所有历史记录
 * @param {object} storage
 */
function clearHistory(storage) {
  storage.removeItem(HISTORY_KEY);
}

// ─── 导出（Node.js / browser 兼容） ───────────────────────────────────────

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    validateFile,
    buildFormFields,
    parseGlossaryInput,
    glossaryToJson,
    mapStatusText,
    mapV2StatusText,
    formatDevice,
    formatJobMeta,
    formatDuration,
    parseMetricsSummary,
    parseTotalMs,
    formatTranslationCost,
    isSafeMode,
    formatErrorCode,
    buildHistoryEntry,
    loadHistory,
    saveHistory,
    clearHistory,
    HISTORY_MAX,
  };
}
