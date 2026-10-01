/** R3: execute the actual inline reporting/eligibility/quote functions offline. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const html = fs.readFileSync(path.resolve(__dirname, "../index.html"), "utf8");
const reportSource = html.slice(html.indexOf("    function renderPrecisionPolishReport(data)"),
  html.indexOf("    function renderQaReport(data)"));
const quoteSource = html.slice(html.indexOf("    let polishQuoteRequestId = 0;"),
  html.indexOf('    enablePrecisionPolish.addEventListener("change"'));

function setup(fetch = async () => { throw Error("Unexpected network"); }) {
  const nodes = new Map();
  function $(id) {
    if (!nodes.has(id)) nodes.set(id, { style: {}, textContent: "", innerHTML: "", value: "simplified" });
    return nodes.get(id);
  }
  const context = { $, API: "", selectedFile: { name: "book.epub" }, selectedFiles: [{}],
    enablePrecisionPolish: { checked: true, disabled: false }, enableTranslation: { checked: false },
    FormData: class { constructor() { this.fields = {}; } append(key, value) { this.fields[key] = value; } }, fetch };
  context.getSelectedFileExt = () => context.selectedFile ? path.extname(context.selectedFile.name) : "";
  vm.createContext(context);
  vm.runInContext(reportSource + quoteSource, context);
  return { context, $ };
}

test("F28-1 完成零修改是有效检查结果", () => {
  const { context, $ } = setup();
  context.renderPrecisionPolishReport({ precision_polish: { status: "completed", reviewed: 5, changed: 0 } });
  assert.ok($("precisionPolishReport").textContent.includes("检查完成"));
  assert.ok($("precisionPolishReport").textContent.includes("无需修改"));
  assert.ok(!$("precisionPolishReport").textContent.includes("退款"));
});

test("F28-2 刷新可从持久统计恢复失败与人工核费提示", () => {
  const { context, $ } = setup();
  context.renderPrecisionPolishReport({ translation_stats: { precision_polish:
    { status: "failed", reviewed: 3, changed: 1, refund_required: true } } });
  assert.ok($("precisionPolishReport").textContent.includes("未完成"));
  assert.ok($("precisionPolishReport").textContent.includes("尚未自动退款"));
  assert.ok($("precisionPolishReport").textContent.includes("已检查 3 段"));
});

test("F28-3 无候选状态不冒充检查完成，服务端内容不能注入HTML", () => {
  const { context, $ } = setup();
  context.renderPrecisionPolishReport({ precision_polish: { status: "no_candidates", reviewed: 0 } });
  assert.ok($("precisionPolishReport").textContent.includes("未发现可精校风险段"));
  assert.ok(!$("precisionPolishReport").textContent.includes("检查完成"));
  context.renderPrecisionPolishReport({ precision_polish: { status: "<img onerror=evil()>", reviewed: "<script>" } });
  assert.equal($("precisionPolishReport").innerHTML, "");
  assert.ok(!$("precisionPolishReport").textContent.includes("<img"));
  context.renderPrecisionPolishReport({});
  assert.equal($("precisionPolishReport").style.display, "none");
  assert.equal($("precisionPolishReport").textContent, "");
});

test("F28-4 只允许单本EPUB普通简体转换，切换后取消勾选", () => {
  for (const change of [c => c.selectedFiles.push({}), c => c.selectedFile.name = "book.md",
    c => c.enableTranslation.checked = true, (c, $) => $("outputMode").value = "traditional"]) {
    const { context, $ } = setup();
    context.syncPrecisionPolishAvailability();
    assert.equal(context.enablePrecisionPolish.disabled, false);
    change(context, $);
    context.syncPrecisionPolishAvailability();
    assert.equal(context.enablePrecisionPolish.disabled, true);
    assert.equal(context.enablePrecisionPolish.checked, false);
    assert.equal($("precisionPolishOptions").style.display, "none");
  }
});

test("F28-5 报价错误展示明确原因，不显示undefined价格", async () => {
  const { context, $ } = setup(async () => ({ ok: false, json: async () => ({ detail: "未发现风险段" }) }));
  await context.estimatePolishPrice();
  assert.equal($("polishPrice").textContent, "估算失败");
  assert.equal($("polishQuoteMessage").textContent, "未发现风险段");
});

test("F28-6 后发报价优先，迟到的旧响应不能覆盖", async () => {
  const pending = [];
  const { context, $ } = setup(() => new Promise(resolve => pending.push(resolve)));
  const first = context.estimatePolishPrice();
  const second = context.estimatePolishPrice();
  pending[1]({ ok: true, json: async () => ({ char_count: 200, price_cny: "5.99" }) });
  await second;
  pending[0]({ ok: true, json: async () => ({ char_count: 100, price_cny: "3.99" }) });
  await first;
  assert.equal($("polishCharCount").textContent, 200);
  assert.equal($("polishPrice").textContent, "5.99");
});

test("F28-7 更换文件或关闭精校后忽略旧响应", async () => {
  for (const change of [c => c.selectedFile = { name: "new.epub" }, c => c.enablePrecisionPolish.checked = false]) {
    let respond;
    const { context, $ } = setup(() => new Promise(resolve => respond = resolve));
    const request = context.estimatePolishPrice();
    change(context);
    respond({ ok: true, json: async () => ({ char_count: 100, price_cny: "3.99" }) });
    await request;
    assert.notEqual($("polishPrice").textContent, "3.99");
  }
});

test("F28-8 所有普通内联脚本语法可解析", () => {
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
  for (const [, attrs, source] of scripts) {
    if (/\bsrc=|type=["']application\/ld\+json/i.test(attrs)) continue;
    new vm.Script(source);
  }
});

test("F28-9 报价携带与下单一致的来源繁体与简体模式", async () => {
  let form;
  const { context, $ } = setup(async (_url, options) => {
    form = options.body;
    return { ok: true, json: async () => ({ char_count: 100, price_cny: "3.99" }) };
  });
  $("traditionalVariant").value = "tw";
  await context.estimatePolishPrice();
  assert.equal(form.fields.output_mode, "simplified");
  assert.equal(form.fields.traditional_variant, "tw");
});

test("F28-10 选单个非EPUB不能覆盖精校不支持状态", () => {
  const { context } = setup();
  Object.assign(context, { returnLater: { setTask() {} }, fileNameEl: { style: {} },
    refreshTaskModeAvailability() {}, isTranslationSupportedForSelectedFile: () => true,
    updateSubmitButton() {}, _updatePriceHint() {}, estimatePolishPrice() {},
    alert: message => { throw Error(message); } });
  const selection = html.slice(html.indexOf("    function selectFiles(files, fromFolder = false)"),
    html.indexOf("    function _updatePriceHint()"));
  vm.runInContext(selection, context);
  context.selectFiles([{ name: "book.md", size: 100 }]);
  assert.equal(context.enablePrecisionPolish.disabled, true);
  assert.equal(context.enablePrecisionPolish.checked, false);
});
