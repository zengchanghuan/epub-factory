/** Execute actual inline quote rendering; no DOM/network dependencies. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const rendering = html.slice(html.indexOf("    function translationPricingView(data = {})"),
  html.indexOf("    function parsePreflightCharacters(text)"));
const payment = html.slice(html.indexOf("    function showTranslationPayment(data)"),
  html.indexOf("    async function confirmTranslationPreflight()"));

function setup() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { textContent: "", innerHTML: "", style: {},
      value: "", checked: false, href: "", scrollIntoView() {} });
    return nodes.get(id);
  };
  const context = { $, document: { querySelectorAll: () => [] },
    TRANSLATION_STRATEGY_LABELS: { neutral_faithful: "中性忠实" },
    escapeHtml: value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    renderSourceWarnings() {}, reportCheckoutEvent() {},
  };
  vm.createContext(context);
  vm.runInContext(rendering + payment, context);
  return { context, $ };
}

test("旧 hit_ratio 和原价差不能显示未经运行验证的节省", () => {
  const { context } = setup();
  const view = context.translationPricingView({ amount: "8.88", estimated_chars: 12500,
    pricing: { hit_ratio: 0.9, cached_chars: 12000, raw_price_cny: "88.80", price_cny: "8.88" } });
  assert.equal(view.amountText, "¥8.88");
  assert(view.detailText.includes("12.5K 字符"));
  assert(view.detailText.includes("以订单已确认金额为准"));
  assert(view.detailText.includes("缓存复用以执行统计为准"));
  assert(!/节省|缓存命中|未预扣/.test(view.detailText));
});

test("v2 deferred 只显示以执行统计为准的固定文本", () => {
  const { context } = setup();
  const view = context.translationPricingView({ amount: "3.99", pricing: {
    schema_version: 2, cache_discount_status: "deferred", total_chars: 40000,
    hit_ratio: 1, message: "已免费翻译，节省100元", cache_discount_message: "<b>免费</b>",
  } });
  assert(view.detailText.includes("40.0K 字符"));
  assert(view.detailText.includes("本次报价未预扣缓存优惠"));
  assert(view.detailText.includes("缓存复用以执行统计为准"));
  assert(!view.detailText.includes("将在"));
  assert(!/免费|100元|<b>/.test(view.detailText));
});

test("fresh disabled 的固定说明不暗示会使用缓存折扣", () => {
  const { context } = setup();
  const view = context.translationPricingView({ pricing: {
    schema_version: 2, cache_discount_status: "disabled", price_cny: "7.98", total_chars: 1234,
  } });
  assert.equal(view.amountText, "¥7.98");
  assert(view.detailText.includes("不复用旧译文缓存，本次不计缓存优惠"));
  assert(!view.detailText.includes("重新翻译"));
});

test("未知 schema/status 包括未来 verified 均不冒认折扣", () => {
  const { context } = setup();
  for (const pricing of [
    { schema_version: "2", cache_discount_status: "deferred" },
    { schema_version: 3, cache_discount_status: "deferred" },
    { schema_version: 2, cache_discount_status: "verified" },
    { schema_version: 2, cache_discount_status: "unknown" },
  ]) {
    const view = context.translationPricingView({ amount: "3.99", pricing: {
      ...pricing, hit_ratio: 1, raw_price_cny: "50", price_cny: "3.99",
    } });
    assert(view.detailText.includes("以订单已确认金额为准"));
    assert(!/节省|缓存命中|未预扣/.test(view.detailText));
  }
});

test("历史订单金额优先于新 pricing 价格，不重算已确认款项", () => {
  const { context } = setup();
  assert.equal(context.translationPricingView({ amount: "19.99", expected_amount: "18.99",
    pricing: { price_cny: "3.99", hit_ratio: 0.8 } }).amountText, "¥19.99");
  assert.equal(context.translationPricingView({ expected_amount: "6.50",
    pricing: { price_cny: "3.99" } }).amountText, "¥6.50");
});

test("无效金额和字符数不会展示NaN或执行后端HTML", () => {
  const { context, $ } = setup();
  for (const value of ["<img src=x onerror=evil()>", "NaN", "Infinity", -5, {}, true, ""]) {
    context.renderTranslationPricing({ amount: value, estimated_chars: value, pricing: {
      schema_version: 2, cache_discount_status: "deferred", message: "<script>evil()</script>",
    } });
    assert.equal($("translationPriceDisplay").textContent, "¥—");
    assert(!/NaN|Infinity|<img|<script>|K 字符/.test($("translationCharDisplay").textContent));
    assert.equal($("translationPriceDisplay").innerHTML, "");
    assert.equal($("translationCharDisplay").innerHTML, "");
  }
});

test("画像创建和刷新共用真实价格文字并不创建付款链接", () => {
  const { context, $ } = setup();
  const quote = { amount: "5.99", estimated_chars: 80000,
    pricing: { schema_version: 2, cache_discount_status: "deferred" } };
  context.renderTranslationPreflight({ profile: {}, glossary: {}, chapters: [] }, "book", false, quote);
  assert($("preflightPricing").textContent.includes("翻译服务价：¥5.99"));
  assert($("preflightPricing").textContent.includes("80.0K 字符"));
  assert($("preflightPricing").textContent.includes("本次报价未预扣缓存优惠"));
  assert.equal($("preflightPricing").innerHTML, "");
  assert.equal($("translationPayLink").href, "");
});

test("确认后支付展示复用真实文字函数且维持既有链接", () => {
  const { context, $ } = setup();
  context.showTranslationPayment({ amount: "3.99", pay_url: "https://payments.invalid/existing-order",
    pricing: { schema_version: 2, cache_discount_status: "deferred", total_chars: 4000 } });
  assert.equal($("translationPriceDisplay").textContent, "¥3.99");
  assert($("translationCharDisplay").textContent.includes("4.0K 字符"));
  assert($("translationCharDisplay").textContent.includes("本次报价未预扣缓存优惠"));
  assert.equal($("translationPayLink").href, "https://payments.invalid/existing-order");
});

test("刷新只读详情显示已保存报价，不恢复或改变付款链接", () => {
  const { context, $ } = setup();
  $("translationPayLink").href = "https://payments.invalid/unchanged";
  $("translationPayArea").style.display = "none";
  context.renderTranslationQuoteSummary({ enable_translation: true, status: "pending_payment",
    amount: "9.90", pricing: { schema_version: 2, cache_discount_status: "disabled", total_chars: 55000 } });
  assert.equal($("translationQuoteSummary").style.display, "block");
  assert($("translationQuoteSummary").textContent.includes("¥9.90 · 约 55.0K 字符"));
  assert($("translationQuoteSummary").textContent.includes("不计缓存优惠"));
  assert.equal($("translationPayLink").href, "https://payments.invalid/unchanged");
  assert.equal($("translationPayArea").style.display, "none");
});

test("切换到转换任务或新文件清理旧翻译报价", () => {
  const { context, $ } = setup();
  context.renderTranslationQuoteSummary({ enable_translation: true, amount: "4.99" });
  context.renderTranslationQuoteSummary({ enable_translation: false, amount: "0.99" });
  assert.equal($("translationQuoteSummary").textContent, "");
  assert.equal($("translationQuoteSummary").style.display, "none");
  context.renderTranslationQuoteSummary();
  assert.equal($("translationQuoteSummary").textContent, "");
});

test("报价展示不修改后端数据或记录中的金额", () => {
  const { context } = setup();
  const data = { amount: "3.99", estimated_chars: 2400, pricing: {
    schema_version: 2, cache_discount_status: "deferred", price_cny: "3.99", cached_chars: 0,
  } };
  const before = JSON.stringify(data);
  context.renderTranslationPricing(data);
  context.renderTranslationQuoteSummary({ ...data, enable_translation: true });
  assert.equal(JSON.stringify(data), before);
});

test("上传、确认和刷新全部接入同一报价展示，移除旧比例优惠分支", () => {
  assert(!html.includes("p.hit_ratio"));
  assert(!html.includes("已为你节省"));
  assert(!html.includes('$("translationCharDisplay").innerHTML'));
  const poll = html.slice(html.indexOf("    function pollJobV2(jobId)"), html.indexOf("    function pollJobV2(jobId)") + 4200);
  assert(poll.includes("renderTranslationQuoteSummary(data)"));
  assert(/renderTranslationPreflight\(\s*data\.translation_preflight,\s*data\.job_id,\s*data\.bilingual,\s*data,/.test(poll));
  assert(/renderTranslationPreflight\(\s*data\.translation_preflight,\s*data\.job_id,\s*\$\("enableBilingual"\)\.checked,\s*data,/.test(html));
  assert(html.includes("if (isTranslationMode) renderTranslationPricing(data)"));
  assert(html.includes('if (data.status === "pending_payment") showTranslationPayment(data)'));
});

test("所有普通内联脚本语法可解析", () => {
  for (const [, attributes, source] of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (/\bsrc=|type=["']application\/ld\+json/i.test(attributes)) continue;
    new vm.Script(source);
  }
});
