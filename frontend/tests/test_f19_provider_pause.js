/** Runtime rendering tests: supplier failure must not masquerade as failed QA. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const lib = require("../lib.js");
const html = fs.readFileSync(path.resolve(__dirname, "../index.html"), "utf8");
const functionSource = html.slice(html.indexOf("    function renderQaReport(data)"),
  html.indexOf("    function retryTranslation(jobId)"));

function render(report) {
  const elements = new Map();
  function $(id) {
    if (!elements.has(id)) elements.set(id, { textContent: "", innerHTML: "", dataset: {},
      classList: { add() {}, remove() {} } });
    return elements.get(id);
  }
  const context = { $, qaReport: $("qaReport"), retryTranslationBtn: $("retryTranslationBtn"),
    translationDiagnosticsBtn: $("translationDiagnosticsBtn"), currentJobId: "job",
    hideQaReport() {}, escapeHtml: text => String(text) };
  vm.createContext(context);
  vm.runInContext(functionSource, context);
  context.renderQaReport({ status: "failed", job_id: "job", qa_report: report });
  return elements;
}

test("F19-1 余额不足显示服务暂停，不显示质检分数", () => {
  const elements = render({ status: "blocked", summary: "模型服务余额不足，缓存已保留",
    score: null, checks: [{ code: "provider_unavailable", message: "模型服务余额不足" }], retryable: true });
  assert.equal(elements.get("qaTitle").textContent, "翻译因模型服务问题暂停");
  assert.ok(elements.get("qaSummary").textContent.includes("余额不足"));
  assert.ok(!elements.get("qaMeta").textContent.includes("质检分"));
  assert.equal(elements.get("retryTranslationBtn").textContent, "服务恢复后继续翻译");
  assert.equal(elements.get("retryTranslationBtn").disabled, false);
});

test("F19-2 真正的内容质量失败仍显示质检失败", () => {
  const elements = render({ status: "failed", score: 60,
    checks: [{ code: "likely_untranslated", message: "1 个段落疑似未翻译" }], retryable: true });
  assert.equal(elements.get("qaTitle").textContent, "翻译质检未通过");
  assert.ok(elements.get("qaMeta").textContent.includes("质检分 60"));
  assert.equal(elements.get("retryTranslationBtn").textContent, "重新翻译");
});

test("F19-3 所有质量档位默认 Flash，显式 Pro 选择仍有效", () => {
  for (const quality of ["standard", "high", "literary"]) {
    assert.equal(lib.buildFormFields({ enableTranslation: true, translationQuality: quality }).translation_model,
      "deepseek-flash");
    assert.equal(lib.buildFormFields({ enableTranslation: true, translationQuality: quality,
      translationModel: "deepseek-v4-pro" }).translation_model, "deepseek-v4-pro");
  }
});

test("F19-4 切换质量档位不覆盖用户选择的模型", () => {
  const handler = html.slice(html.indexOf("    translationQualityChoices.forEach(radio => {"),
    html.indexOf("    translationModelChoices.forEach(radio => {"));
  assert.ok(handler.includes('$("translationQuality").value = quality'));
  assert.ok(!handler.includes('$("translationModel").value ='));
});
