/**
 * 测试 F10：转换与 AI 翻译互斥模式
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
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

test("F10-4 MOBI/AZW3 禁用翻译模式", () => {
  assert.ok(html.includes('isTranslationSupportedForSelectedFile'), "应有文件类型判断");
  assert.ok(html.includes('[".epub", ".pdf", ".docx", ".md", ".markdown"].includes(ext)'), "仅可翻译合适文件类型");
  assert.ok(html.includes('translationRadio.disabled = !supported'), "不支持时应禁用翻译模式");
});
