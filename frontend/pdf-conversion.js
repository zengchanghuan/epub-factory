/* Dedicated, capability-gated PDF workflow. No PDF content is rendered here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FixEpubPdfConversion = api;
})(typeof window === "undefined" ? globalThis : window, function () {
  "use strict";
  const WARNING_LABELS = Object.freeze({
    empty_password_encryption: "原文件带有可直接打开的加密标记，转换将保留可读取的内容。",
    pages_without_extractable_text: "部分页面没有可提取文字；不会进行 OCR，原有可支持图片会保留。",
    late_text_overlay_preserved: "检测到后加的叠加文字，将原样保留，不自动删除或改写。",
    memory_limit_unavailable: "本次处理未能启用硬内存限制，暂不可确认收费。",
    epubcheck_warnings: "成品仍有格式检查警告，暂不可确认收费。",
  });
  const BLOCKED_LABELS = Object.freeze({
    memory_limit_unavailable: "处理环境暂不满足硬内存限制，请稍后重试或联系客服。",
    epubcheck_warnings: "成品存在格式检查警告，暂不能创建支付订单。",
    service_disabled: "PDF 转换暂未开放，请稍后重试。",
    unsupported_warning: "存在尚不能确认的风险，请联系客服。",
    validation_failed: "成品未通过格式检查，不能创建支付订单。",
    review_blocked: "此 PDF 仍有不能通过勾选解除的风险，暂不能创建支付订单。",
    invalid_plan: "转换方案校验失败，请刷新任务或联系客服。",
  });
  const money = value => typeof value === "string" && /^\d{1,6}(?:\.\d{1,2})?$/.test(value)
    ? Number(value).toFixed(2) : null;
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  function capability(payload) {
    const c = payload && payload.pdf_text_conversion;
    if (!c || c.enabled !== true || c.preserves_original !== true || !money(c.price_cny)
      || !Number.isSafeInteger(c.max_file_size_mb) || c.max_file_size_mb <= 0
      || !Number.isSafeInteger(c.max_file_size_mb * 1024 * 1024)
      || !Number.isSafeInteger(c.max_pages) || c.max_pages <= 0) return { enabled: false };
    return { enabled: true, price: money(c.price_cny), maxBytes: c.max_file_size_mb * 1024 * 1024,
      maxFileSizeMb: c.max_file_size_mb, maxPages: c.max_pages };
  }
  function validateFiles(files, cap) {
    if (!cap || cap.enabled !== true) return "PDF 转换暂未开放。";
    const list = Array.from(files || []);
    if (list.length !== 1) return "PDF 转换仅支持单个文件，不支持批量或文件夹。";
    if (typeof list[0].name !== "string" || !/\.pdf$/i.test(list[0].name)) return "请选择一个 PDF 文件。";
    if (!Number.isSafeInteger(list[0].size) || list[0].size <= 0) return "文件为空或无法读取。";
    if (list[0].size > cap.maxBytes) return `PDF 文件不能超过 ${cap.maxFileSizeMb} MB。`;
    return "";
  }
  function view(data) {
    const s = data && data.pdf_conversion;
    if (!s || typeof s !== "object" || Array.isArray(s) || s.preserves_original !== true) return null;
    const warnings = Array.isArray(s.warnings) && s.warnings.length <= 100
      && s.warnings.every(w => typeof w === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(w))
      && new Set(s.warnings).size === s.warnings.length ? s.warnings.slice() : null;
    const amount = money(s.amount);
    const plan = typeof s.plan_id === "string" && /^[a-f0-9]{64}$/.test(s.plan_id) ? s.plan_id : "";
    const unknown = warnings === null || warnings.some(w => !Object.hasOwn(WARNING_LABELS, w));
    const safe = warnings !== null && !unknown && amount !== null && !!plan;
    const awaiting = data.status === "awaiting_confirmation";
    const canConfirm = safe && awaiting && s.phase === "prepared" && s.confirmed === false && s.can_confirm === true && !s.blocked_reason
      && !warnings.some(w => ["memory_limit_unavailable", "epubcheck_warnings"].includes(w));
    return { awaiting, canConfirm, phase: s.phase, planId: plan, amount,
      pageCount: count(s.page_count), characters: count(s.normalized_characters),
      imageAssets: count(s.image_assets), tocEntries: count(s.toc_entries),
      warnings: (warnings || []).map(code => ({ code, label: Object.hasOwn(WARNING_LABELS, code) ? WARNING_LABELS[code] : "存在未知风险，请联系客服确认。" })),
      blockedMessage: canConfirm ? "" : ((Object.hasOwn(BLOCKED_LABELS, s.blocked_reason) ? BLOCKED_LABELS[s.blocked_reason] : "") ||
        (awaiting ? "当前结果暂不可确认，请刷新任务或联系客服。" : "")) };
  }
  function confirmationBody(data, accepted) {
    const state = view(data);
    if (!state || !state.canConfirm) throw new Error("当前 PDF 结果不可确认，请刷新任务状态。");
    if (!Array.isArray(accepted) || new Set(accepted).size !== accepted.length
      || accepted.length !== state.warnings.length || state.warnings.some(w => !accepted.includes(w.code))) {
      throw new Error("请逐项阅读并确认全部风险提示。");
    }
    return { plan_id: state.planId, accepted_warnings: state.warnings.map(w => w.code) };
  }
  function createClient(options) {
    let uploading = false;
    const confirming = new Set();
    const url = path => (options.api || "") + path;
    const headers = () => options.headers ? options.headers() : {};
    async function decode(response) {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(response.status === 409
        ? "任务方案或状态已变化，请刷新后重新确认。"
        : response.status === 503 ? "PDF 转换暂不可用，请稍后重试。" : "PDF 请求未完成，请刷新任务状态后重试。");
      return data;
    }
    return {
      async capabilities() {
        try { return capability(await decode(await options.fetch(url("/api/v2/capabilities"), { cache: "no-store" }))); }
        catch (_) { return { enabled: false }; }
      },
      async create(files, cap) {
        const error = validateFiles(files, cap);
        if (error) throw new Error(error);
        if (uploading) throw new Error("PDF 正在上传，请勿重复提交。");
        uploading = true;
        try {
          const form = new options.FormData();
          form.append("file", Array.from(files)[0]);
          const key = options.adminKey && options.adminKey();
          if (key) form.append("admin_key", key);
          return await decode(await options.fetch(url("/api/v2/pdf-jobs"), { method: "POST", headers: headers(), body: form }));
        } finally { uploading = false; }
      },
      async confirm(jobId, data, accepted) {
        if (typeof jobId !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(jobId)) throw new Error("任务标识无效。");
        const body = confirmationBody(data, accepted);
        if (confirming.has(jobId)) throw new Error("确认请求正在处理中，请勿重复提交。");
        confirming.add(jobId);
        try {
          return await decode(await options.fetch(url(`/api/v2/jobs/${encodeURIComponent(jobId)}/confirm-conversion`), {
            method: "POST", headers: { ...headers(), ...(options.authHeaders ? options.authHeaders(jobId) : {}), "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }));
        } finally { confirming.delete(jobId); }
      },
    };
  }
  return { capability, validateFiles, view, confirmationBody, createClient, WARNING_LABELS };
});
