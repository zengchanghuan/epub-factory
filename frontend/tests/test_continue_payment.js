/** Actual inline payment recovery controller; all HTTP/popups are controlled. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const controller = html.slice(html.indexOf("    function openAlipayPopup(event)"),
  html.indexOf("    function taskVerb("));
const polling = html.slice(html.indexOf("    function pollBatchV2(batchId)"),
  html.indexOf('    navUpload.addEventListener("click"'));
const defer = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const pending = (id = "job-a", extra = {}) => ({ job_id: id, status: "pending_payment", checkout_available: true,
  pay_url: "https://payments.invalid/original-order", amount: "9.90", ...extra });
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

function setup() {
  const nodes = new Map(), calls = [], popups = [], polls = [], tokens = new Map(), events = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { textContent: "", innerHTML: "", href: "#", hidden: true,
      disabled: false, style: {}, dataset: {}, listeners: {}, value: "", checked: false,
      classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; },
      getAttribute(name) { return this[name]; }, removeAttribute(name) { delete this[name]; },
      scrollIntoView() {}, dispatchEvent() {},
    });
    return nodes.get(id);
  };
  const context = { $, URL, Set, API: "", currentJobId: "job-a", currentBatchId: null,
    currentJobStatus: "pending_payment", continuePaymentGeneration: 0, continuePaymentInFlight: false,
    continuePaymentTarget: null, continuePaymentRequests: new Set(), pollTimer: null, alipayPopup: null,
    errorMsg: $("errorMsg"), authHeaders: key => ({ "X-Test-Auth-Key": key }),
    fetch: async (url, options = {}) => { calls.push({ url, options }); return context.respond(url, options); },
    respond: () => response(pending()),
    getJobToken: key => tokens.get(key), saveJobToken: (key, token) => tokens.set(key, token),
    setInterval(fn, ms) { polls.push({ timer: true, ms }); return 42; }, clearInterval() {},
    pollJobV2: id => polls.push({ kind: "job", id }), pollBatchV2: id => polls.push({ kind: "batch", id }),
    renderTranslationQuoteSummary: data => events.push({ quote: data }), loadTaskList: () => events.push("list"),
    reportCheckoutEvent: event => events.push(event),
    window: { screenX: 0, screenY: 0, outerWidth: 1200, outerHeight: 900,
      open(...args) { popups.push(args); return { focus() {}, close() { events.push("close"); } }; } },
  };
  context.openJobDetail = id => {
    context.resetContinuePayment(); context.currentBatchId = null; context.currentJobId = id;
    context.currentJobStatus = ""; events.push({ openJob: id });
  };
  context.openBatchDetail = id => {
    context.resetContinuePayment(); context.currentJobId = null; context.currentBatchId = id;
    context.currentJobStatus = ""; events.push({ openBatch: id });
  };
  vm.createContext(context);
  vm.runInContext(controller, context);
  context.renderContinuePayment(pending(), "job", "job-a");
  return { context, $, calls, popups, polls, tokens, events };
}

test("刷新待支付详情只展示显式按钮，不自动查单或打开支付宝", () => {
  const { context, $, calls, popups } = setup();
  for (let i = 0; i < 5; i++) context.renderContinuePayment(pending(), "job", "job-a");
  assert.equal($("continuePaymentPanel").style.display, "block");
  assert.equal($("continuePaymentBtn").textContent, "继续支付");
  assert.equal(calls.length, 0); assert.equal(popups.length, 0);
  assert(!polling.includes("/continue-payment"));
  assert(!polling.includes("continuePendingPayment("));
});

test("原订单成功查单才显示链接与冻结金额，第二次用户点击才开小窗口", async () => {
  const { context, $, calls, popups, polls } = setup();
  await context.continuePendingPayment();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v2/jobs/job-a/continue-payment");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.headers["X-Test-Auth-Key"], "job-a");
  assert.equal($("continuePaymentLink").hidden, false);
  assert($("continuePaymentMessage").textContent.includes("¥9.90"));
  assert.equal(popups.length, 0);
  assert(polls.some(p => p.kind === "job" && p.id === "job-a"));
  let prevented = false;
  $("continuePaymentLink").listeners.click({ currentTarget: $("continuePaymentLink"), preventDefault() { prevented = true; } });
  assert(prevented); assert.equal(popups.length, 1);
  assert.equal(popups[0][1], "fixepub-alipay");
  assert(popups[0][2].includes("width=520,height=720"));
});

test("请求进行中重复点击及列表重绘按钮不会重复 POST", async () => {
  const { context, $, calls } = setup(); const gate = defer();
  context.respond = () => gate.promise;
  const first = context.continuePendingPayment();
  assert.equal($("continuePaymentBtn").disabled, true);
  await context.continuePendingPayment();
  await context.continuePaymentFromTask("job-a");
  assert.equal(calls.length, 1);
  gate.resolve(response(pending())); await first;
  assert.equal($("continuePaymentBtn").disabled, false);
  assert.equal(context.continuePaymentRequests.size, 0);
});

test("即使离开再回同一任务，进行中的同订单请求也不能重复发起", async () => {
  const { context, calls, $ } = setup(); const gate = defer(); context.respond = () => gate.promise;
  const first = context.continuePendingPayment();
  context.openJobDetail("job-b"); context.openJobDetail("job-a");
  context.currentJobStatus = "pending_payment"; context.renderContinuePayment(pending(), "job", "job-a");
  await context.continuePendingPayment(); assert.equal(calls.length, 1);
  gate.resolve(response(pending())); await first;
  assert.equal($("continuePaymentLink").hidden, true);
  assert.equal(context.continuePaymentRequests.size, 0);
});

test("503、409、鉴权失败和断网清除旧链接二维码，不恢复或新建订单", async () => {
  for (const status of [503, 409, 403, 401, "network"]) {
    const { context, $, calls } = setup();
    $("continuePaymentLink").hidden = false; $("continuePaymentLink").href = "https://payments.invalid/stale";
    $("translationPayLink").href = "https://payments.invalid/stale"; $("conversionQrImg").src = "data:image/png;base64,old";
    context.respond = () => { if (status === "network") throw new Error("连接失败"); return response({ detail: "订单暂不可付款" }, status); };
    await context.continuePendingPayment();
    assert.equal($("continuePaymentLink").hidden, true); assert.equal($("continuePaymentLink").href, "#");
    assert.equal($("translationPayLink").href, "#"); assert.equal($("conversionQrImg").src, undefined);
    assert.equal($("conversionPayArea").style.display, "none");
    assert.equal($("continuePaymentBtn").disabled, false); assert($("continuePaymentMessage").textContent);
    assert.equal(context.currentJobId, "job-a"); assert.equal(calls.length, 1);
  }
});

test("已付、处理中与完成返回不恢复付款并恢复只读轮询", async () => {
  for (const status of ["queued", "running", "completed", "failed", "cancelled"]) {
    const { context, $, polls, events } = setup();
    let closed = false; context.alipayPopup = { closed: false, close() { closed = true; } };
    context.respond = () => response(pending("job-a", { status, checkout_available: false }));
    await context.continuePendingPayment();
    assert.equal(context.currentJobStatus, status); assert.equal($("continuePaymentLink").hidden, true);
    assert.equal($("continuePaymentPanel").style.display, "none"); assert(closed);
    assert(events.includes("list")); assert(polls.some(p => p.kind === "job"));
  }
});

test("仅 checkout_available 真布尔与安全绝对 http(s) 链接可付款", async () => {
  for (const data of [
    { checkout_available: false }, { checkout_available: "true" }, { pay_url: "javascript:alert(1)" },
    { pay_url: "data:text/html,x" }, { pay_url: "/relative" }, { pay_url: "//payments.invalid/pay" },
    { pay_url: "https://user:secret@payments.invalid/pay" }, { pay_url: "https://user@payments.invalid/pay" },
    { pay_url: null }, { pay_url: {} }, { job_id: "wrong-order" },
  ]) {
    const { context, $ } = setup(); context.respond = () => response(pending("job-a", data));
    await context.continuePendingPayment(); assert.equal($("continuePaymentLink").hidden, true);
    assert.equal($("continuePaymentLink").href, "#");
  }
  const { context } = setup(); assert.equal(context.safeCheckoutUrl("http://payments.invalid/pay"), "http://payments.invalid/pay");
});

test("旧任务成功或失败响应不能覆盖新任务面板", async () => {
  for (const status of [200, 503]) {
    const { context, $ } = setup(); const gate = defer(); context.respond = () => gate.promise;
    const first = context.continuePendingPayment();
    context.openJobDetail("job-b"); context.currentJobStatus = "pending_payment";
    context.renderContinuePayment(pending("job-b"), "job", "job-b");
    $("continuePaymentMessage").textContent = "新任务面板";
    gate.resolve(response(pending(), status)); await first;
    assert.equal(context.currentJobId, "job-b"); assert.equal($("continuePaymentMessage").textContent, "新任务面板");
    assert.equal($("continuePaymentLink").hidden, true);
  }
});

test("付款已被较新 GET 确认时，旧未付响应不能重新打开付款", async () => {
  const { context, $ } = setup(); const gate = defer(); context.respond = () => gate.promise;
  const first = context.continuePendingPayment();
  context.currentJobStatus = "running"; context.renderContinuePayment({ status: "running" }, "job", "job-a");
  gate.resolve(response(pending())); await first;
  assert.equal(context.currentJobStatus, "running"); assert.equal($("continuePaymentPanel").style.display, "none");
  assert.equal($("continuePaymentLink").hidden, true);
});

test("批次使用原 batch endpoint、授权键和整批付款提示", async () => {
  const { context, $, calls } = setup(); context.openBatchDetail("batch-a");
  context.currentJobStatus = "pending_payment"; context.renderContinuePayment({ status: "pending_payment" }, "batch", "batch-a");
  context.respond = () => response({ ...pending(), batch_id: "batch-a", amount: "2.97", qr_code: null });
  await context.continuePendingPayment();
  assert.equal(calls[0].url, "/api/v2/batches/batch-a/continue-payment");
  assert.equal(calls[0].options.headers["X-Test-Auth-Key"], "batch:batch-a");
  assert($("continuePaymentLink").textContent.includes("整批订单"));
  assert($("continuePaymentMessage").textContent.includes("¥2.97"));
});

test("原扫码订单只在显式查单后用本地 QRCode 呈现原码，不打开网页", async () => {
  for (const batch of [false, true]) {
    const { context, $, calls, popups } = setup(); const generated = [];
    context.QRCode = { async toDataURL(value, options) { generated.push({ value, options }); return "data:image/png;base64,YWJj"; } };
    if (batch) {
      context.openBatchDetail("batch-a"); context.currentJobStatus = "pending_payment";
      context.renderContinuePayment({ status: "pending_payment" }, "batch", "batch-a");
    }
    context.respond = () => response(pending("job-a", { batch_id: batch ? "batch-a" : null,
      pay_url: null, qr_code: "https://qr.alipay.invalid/original?order=unchanged", amount: "0.99" }));
    assert.equal(generated.length, 0); assert.equal($("continuePaymentQr").hidden, true);
    await context.continuePendingPayment();
    assert.equal(generated.length, 1); assert.equal(generated[0].value, "https://qr.alipay.invalid/original?order=unchanged");
    assert.equal(generated[0].options.width, 200); assert.equal(generated[0].options.margin, 1);
    assert.equal($("continuePaymentQr").hidden, false); assert.equal($("continuePaymentQr").src, "data:image/png;base64,YWJj");
    assert.equal($("continuePaymentLink").hidden, true); assert.equal($("continuePaymentLink").href, "#");
    assert($("continuePaymentMessage").textContent.includes("支付宝扫描二维码"));
    assert($("continuePaymentMessage").textContent.includes("¥0.99")); assert.equal(popups.length, 0); assert.equal(calls.length, 1);
  }
});

test("混合通道、空通道和危险二维码 URL 均不编码不开放付款", async () => {
  for (const fields of [
    { qr_code: "https://qr.alipay.invalid/original" },
    { pay_url: "", qr_code: "https://qr.alipay.invalid/original" },
    { pay_url: null, qr_code: null }, { pay_url: null, qr_code: "" },
    { pay_url: null, qr_code: "javascript:alert(1)" }, { pay_url: null, qr_code: "data:text/html,evil" },
    { pay_url: null, qr_code: "/relative-qr" }, { pay_url: null, qr_code: "//qr.alipay.invalid/pay" },
    { pay_url: null, qr_code: "https://user:secret@qr.alipay.invalid/pay" },
    { pay_url: null, qr_code: "https://user@qr.alipay.invalid/pay" }, { pay_url: null, qr_code: {} },
  ]) {
    const { context, $ } = setup(); let generated = false;
    context.QRCode = { async toDataURL() { generated = true; return "data:image/png;base64,YWJj"; } };
    context.respond = () => response(pending("job-a", fields));
    await context.continuePendingPayment(); assert.equal(generated, false);
    assert.equal($("continuePaymentQr").hidden, true); assert.equal($("continuePaymentQr").src, undefined);
    assert.equal($("continuePaymentLink").hidden, true);
  }
});

test("二维码库缺失、报错或非本地 PNG 输出明确提示且不静默切换支付通道", async () => {
  for (const library of [undefined, {}, { toDataURL: async () => { throw new Error("internal"); } },
    { toDataURL: async () => "https://remote.invalid/qr.png" }, { toDataURL: async () => "data:image/svg+xml;base64,YWJj" }]) {
    const { context, $, calls } = setup(); context.QRCode = library;
    context.respond = () => response(pending("job-a", { pay_url: null, qr_code: "https://qr.alipay.invalid/original" }));
    await context.continuePendingPayment();
    assert($("continuePaymentMessage").textContent.includes("二维码"));
    assert(/未就绪|生成失败/.test($("continuePaymentMessage").textContent));
    assert.equal($("continuePaymentQr").hidden, true); assert.equal($("continuePaymentQr").src, undefined);
    assert.equal($("continuePaymentLink").hidden, true); assert.equal(calls.length, 1);
  }
});

test("新请求、错误、已付款和切换任务均清除旧二维码", async () => {
  for (const change of ["new", "error", "paid", "switch"]) {
    const { context, $ } = setup();
    $("continuePaymentQr").src = "data:image/png;base64,b2xk"; $("continuePaymentQr").hidden = false;
    if (change === "switch") context.openJobDetail("job-b");
    else if (change === "paid") {
      context.currentJobStatus = "running"; context.renderContinuePayment({ status: "running" }, "job", "job-a");
    } else {
      const gate = defer(); context.respond = () => gate.promise; const work = context.continuePendingPayment();
      assert.equal($("continuePaymentQr").hidden, true); assert.equal($("continuePaymentQr").src, undefined);
      gate.resolve(change === "error" ? response({ detail: "不可继续付款" }, 409) : response(pending())); await work;
    }
    assert.equal($("continuePaymentQr").hidden, true); assert.equal($("continuePaymentQr").src, undefined);
  }
});

test("二维码编码等待时仍禁止连点，编码迟到不能覆盖新任务或已付款状态", async () => {
  for (const change of ["switch", "paid"]) {
    const { context, $, calls } = setup(); const gate = defer();
    context.QRCode = { toDataURL: () => gate.promise };
    context.respond = () => response(pending("job-a", { pay_url: null, qr_code: "https://qr.alipay.invalid/original" }));
    const first = context.continuePendingPayment(); await tick();
    assert.equal($("continuePaymentBtn").disabled, true); await context.continuePendingPayment(); assert.equal(calls.length, 1);
    if (change === "switch") context.openJobDetail("job-b");
    else { context.currentJobStatus = "running"; context.renderContinuePayment({ status: "running" }, "job", "job-a"); }
    gate.resolve("data:image/png;base64,YWJj"); await first;
    assert.equal($("continuePaymentQr").hidden, true); assert.equal($("continuePaymentQr").src, undefined);
    assert.equal($("continuePaymentLink").hidden, true);
  }
});

test("任务列表子单先读取详情再转到父批次，绝不调用子单续付", async () => {
  const { context, calls, tokens, events } = setup(); tokens.set("child", "capability");
  context.respond = url => response(url.endsWith("/continue-payment")
    ? { ...pending(), batch_id: "batch-a" } : pending("child", { batch_id: "batch-a" }));
  await context.continuePaymentFromTask("child");
  assert.deepEqual(calls.map(c => c.url), ["/api/v2/jobs/child", "/api/v2/batches/batch-a/continue-payment"]);
  assert.equal(tokens.get("batch:batch-a"), "capability");
  assert(events.some(e => e.openBatch === "batch-a"));
  assert.equal(context.currentJobId, null); assert.equal(context.currentBatchId, "batch-a");
});

test("子单不能覆盖已经保存的父批次凭据", async () => {
  const { context, tokens, calls } = setup(); tokens.set("child", "child-cap"); tokens.set("batch:batch-a", "parent-cap");
  context.respond = url => response(url.endsWith("/continue-payment")
    ? { ...pending(), batch_id: "batch-a" } : pending("child", { batch_id: "batch-a" }));
  await context.continuePaymentFromTask("child");
  assert.equal(tokens.get("batch:batch-a"), "parent-cap"); assert.equal(calls.length, 2);
});

test("旧子单详情迟到或详情身份不符时不能切换当前批次或发起付款", async () => {
  const { context, calls } = setup(); const gate = defer(); context.respond = () => gate.promise;
  const first = context.continuePaymentFromTask("child"); context.openJobDetail("job-b");
  gate.resolve(response(pending("child", { batch_id: "old-batch" }))); await first;
  assert.equal(context.currentJobId, "job-b"); assert.equal(context.currentBatchId, null); assert.equal(calls.length, 1);
  const second = setup(); second.context.respond = () => response(pending("wrong"));
  await second.context.continuePaymentFromTask("child"); assert.equal(second.calls.length, 1);
  assert(second.$("errorMsg").textContent.includes("不一致"));
});

test("列表中已变已付的任务不发续付 POST", async () => {
  const { context, calls, $ } = setup(); context.respond = () => response(pending("job-a", { status: "running" }));
  await context.continuePaymentFromTask("job-a");
  assert.equal(calls.length, 1); assert(!calls[0].options.method); assert.equal($("continuePaymentLink").hidden, true);
});

test("错误文本与金额安全展示，任何后端 HTML 不注入 DOM", async () => {
  const { context, $ } = setup(); context.respond = () => response({ detail: "<img src=x onerror=evil()>" }, 503);
  await context.continuePendingPayment(); assert.equal($("continuePaymentMessage").innerHTML, "");
  assert.equal($("continuePaymentMessage").textContent, "<img src=x onerror=evil()>");
  context.respond = () => response(pending("job-a", { amount: true }));
  await context.continuePendingPayment(); assert(!$("continuePaymentMessage").textContent.includes("¥"));
});

test("单本和批次旧 GET 的成功/失败不覆盖切换后页面", async () => {
  for (const kind of ["job", "batch"]) for (const reject of [false, true]) {
    const { context, $ } = setup(); const gate = defer(); context.respond = () => gate.promise;
    vm.runInContext(polling, context);
    if (kind === "batch") { context.currentBatchId = "batch-a"; context.currentJobId = null; context.pollBatchV2("batch-a"); }
    else context.pollJobV2("job-a");
    context.openJobDetail("job-b"); $("continuePaymentMessage").textContent = "新任务";
    if (reject) gate.reject(new Error("旧任务错误")); else gate.resolve(response(pending()));
    await tick(); assert.equal($("continuePaymentMessage").textContent, "新任务");
    assert.equal($("errorMsg").textContent, ""); assert.equal(context.currentJobId, "job-b");
  }
});

test("同任务离开再回来后旧 GET 也被 generation 拒绝", async () => {
  const { context, $ } = setup(); const gate = defer(); context.respond = () => gate.promise;
  vm.runInContext(polling, context); context.pollJobV2("job-a");
  context.openJobDetail("job-b"); context.openJobDetail("job-a");
  gate.resolve(response(pending())); await tick();
  assert.equal(context.currentJobStatus, ""); assert.equal($("continuePaymentPanel").style.display, "none");
});

test("原生面板可见、列表待支付入口、已有支付成功 recover 均保留", () => {
  const panelStart = html.indexOf('<div id="continuePaymentPanel"');
  assert(panelStart > html.indexOf('id="statusCard"'));
  assert(html.includes('j.status === "pending_payment" ? "继续支付" : "查看"'));
  assert(html.includes('e.target.closest(".tasks-continue-payment-btn")'));
  assert(html.includes('$("continuePaymentBtn").addEventListener("click", continuePendingPayment)'));
  assert(html.includes('/api/v2/jobs/${jobId}/recover'));
  assert(html.includes('/api/v2/batches/${batchId}/recover'));
  assert(!controller.includes("localStorage")); assert(!controller.includes("sessionStorage"));
  assert(!controller.includes("window.location")); assert(!controller.includes('method: "POST", body:'));
});

test("所有普通内联脚本保持可解析", () => {
  for (const [, attributes, source] of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (/\bsrc=|type=["']application\/ld\+json/i.test(attributes)) continue;
    new vm.Script(source);
  }
});
