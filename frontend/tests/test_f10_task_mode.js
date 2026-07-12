/**
 * 测试 F10：转换与 AI 翻译互斥模式
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);
const repairHtml = fs.readFileSync(
  path.resolve(__dirname, "../epub-repair.html"),
  "utf-8"
);

test("F10-1 使用任务模式二选一入口", () => {
  assert.ok(html.includes('name="taskMode" value="convert"'), "应有转换模式");
  assert.ok(html.includes('name="taskMode" value="translate"'), "应有 AI 翻译模式");
  assert.ok(html.includes('id="conversionOptions"'), "转换设置应在独立面板");
  assert.ok(html.includes('id="translationDetails"'), "翻译设置应在独立面板");
});

test("F10-2 AI 精校从属于转换模式", () => {
  const conversionStart = html.indexOf('id="conversionOptions"');
  const polish = html.indexOf('id="enablePrecisionPolish"');
  const translationStart = html.indexOf('id="translationDetails"');
  assert.ok(conversionStart >= 0 && polish > conversionStart, "AI 精校应位于转换面板内");
  assert.ok(translationStart > polish, "AI 翻译面板应在转换面板之后");
  assert.ok(html.includes('if (enablePrecisionPolish.checked) setTaskMode("convert")'), "勾选精校应保持转换模式");
});

test("F10-3 提交参数按当前模式互斥", () => {
  assert.ok(html.includes('const isTranslationMode = getTaskMode() === "translate"'), "提交时应读取任务模式");
  assert.ok(html.includes('form.append("enable_precision_polish", !isTranslationMode && enablePrecisionPolish.checked)'), "翻译模式不应发送精校");
  assert.ok(html.includes('form.append("enable_translation", isTranslationMode)'), "AI 翻译参数应由任务模式决定");
});

test("F10-4 AI 翻译可选择模型", () => {
  assert.ok(html.includes('class="model-picker"'), "模型选择区域应更醒目");
  assert.ok(html.includes('id="translationModel" value="deepseek-v4-flash"'), "默认应使用 flash 模型");
  assert.ok(html.includes('name="translationModelChoice" value="deepseek-v4-flash" checked'), "Flash radio 应默认选中");
  assert.ok(html.includes('class="model-current"'), "应有醒目的当前模型标记");
  assert.ok(html.includes('value="deepseek-v4-pro"'), "应可选择 pro 模型");
  assert.ok(html.includes('value="deepseek-v4-flash"'), "应可切换 flash 模型");
  assert.ok(html.includes('translationModelChoices.forEach'), "应同步模型选择");
  assert.ok(html.includes('form.append("translation_model", $("translationModel").value || "deepseek-v4-flash")'), "提交时应传 translation_model");
});

test("F10-5 MOBI/AZW3 禁用翻译模式", () => {
  assert.ok(html.includes('isTranslationSupportedForSelectedFile'), "应有文件类型判断");
  assert.ok(html.includes('[".epub", ".pdf", ".docx", ".md", ".markdown"].includes(ext)'), "仅可翻译合适文件类型");
  assert.ok(html.includes('translationRadio.disabled = !supported'), "不支持时应禁用翻译模式");
});

test("F10-6 支付宝付款在当前页面打开", () => {
  assert.ok(
    html.includes('id="translationPayLink" href="#" target="_self"'),
    "AI 翻译支付链接应复用当前页面"
  );
  assert.ok(
    html.includes("支付完成后将返回本页面，并自动开始任务"),
    "主页面应说明支付完成后会返回"
  );
  assert.ok(
    repairHtml.includes('href="${escHtml(data.pay_url)}" target="_self"'),
    "修复页降级支付链接应复用当前页面"
  );
  assert.ok(
    repairHtml.includes("支付完成后将返回本页面，并自动检测和开始修复"),
    "修复页应说明支付完成后会返回"
  );
});
