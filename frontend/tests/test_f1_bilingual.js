/**
 * F1 测试：双语对照开关
 *
 * 测试用例：
 * 1. 未开翻译时，buildFormFields 不含 bilingual 字段
 * 2. 开翻译但未开双语，bilingual = "false"
 * 3. 开翻译且开双语，bilingual = "true"
 * 4. 双语开关不影响 output_mode 和 device
 * 5. formatJobMeta 翻译关闭时不含双语文字
 * 6. formatJobMeta 翻译开启+单语，含 AI翻译 但不含 双语并排
 * 7. formatJobMeta 翻译开启+双语，含 双语并排
 * 8. validateFile 对合法文件返回 valid=true
 * 9. validateFile 对非法文件返回 valid=false
 */

const lib = require("../lib");
const { buildFormFields, formatJobMeta, validateFile } = lib;

// ─── buildFormFields ───────────────────────────────────────────────────────

test("1. 未开翻译时不含 bilingual 字段", () => {
  const fields = buildFormFields({
    outputMode: "simplified",
    device: "generic",
    enableTranslation: false,
    bilingual: true, // 即使设了，也不应出现
  });
  assert.strictEqual("bilingual" in fields, false);
});

test("2. 开翻译但未开双语 → bilingual=false", () => {
  const fields = buildFormFields({
    outputMode: "simplified",
    device: "kindle",
    enableTranslation: true,
    targetLang: "zh-CN",
    bilingual: false,
  });
  assert.strictEqual(fields.bilingual, "false");
});

test("3. 开翻译且开双语 → bilingual=true", () => {
  const fields = buildFormFields({
    outputMode: "simplified",
    device: "generic",
    enableTranslation: true,
    targetLang: "zh-CN",
    bilingual: true,
  });
  assert.strictEqual(fields.bilingual, "true");
});

test("4. 双语开关不影响 output_mode 和 device", () => {
  const fields = buildFormFields({
    outputMode: "traditional",
    device: "apple",
    enableTranslation: true,
    bilingual: true,
  });
  assert.strictEqual(fields.output_mode, "traditional");
  assert.strictEqual(fields.device, "apple");
});

test("5. enable_translation 默认 false 时字段值为 'false'", () => {
  const fields = buildFormFields({
    outputMode: "simplified",
    device: "generic",
    enableTranslation: false,
  });
  assert.strictEqual(fields.enable_translation, "false");
});

// ─── formatJobMeta ─────────────────────────────────────────────────────────

test("6. 翻译关闭时，formatJobMeta 不含双语文字", () => {
  const meta = formatJobMeta({
    output_mode: "simplified",
    device: "generic",
    enable_translation: false,
    bilingual: false,
  });
  assert.ok(!meta.includes("双语"), `不应含双语，实际: ${meta}`);
  assert.ok(!meta.includes("AI翻译"), `不应含AI翻译，实际: ${meta}`);
});

test("7. 翻译开启+单语，含 AI翻译 但不含 双语并排", () => {
  const meta = formatJobMeta({
    output_mode: "simplified",
    device: "kindle",
    enable_translation: true,
    target_lang: "zh-CN",
    bilingual: false,
  });
  assert.ok(meta.includes("AI翻译"), `应含 AI翻译，实际: ${meta}`);
  assert.ok(!meta.includes("双语并排"), `不应含 双语并排，实际: ${meta}`);
});

test("8. 翻译开启+双语，含 双语并排", () => {
  const meta = formatJobMeta({
    output_mode: "simplified",
    device: "generic",
    enable_translation: true,
    target_lang: "zh-CN",
    bilingual: true,
  });
  assert.ok(meta.includes("双语并排"), `应含 双语并排，实际: ${meta}`);
});

// ─── validateFile ──────────────────────────────────────────────────────────

test("9. validateFile 接受 .epub 文件", () => {
  const result = validateFile("book.epub");
  assert.strictEqual(result.valid, true);
});

test("10. validateFile 接受 .pdf 文件", () => {
  const result = validateFile("doc.PDF"); // 大小写
  assert.strictEqual(result.valid, true);
});

test("11. validateFile 拒绝 .docx 文件", () => {
  const result = validateFile("book.docx");
  assert.strictEqual(result.valid, false);
  assert.ok(result.error && result.error.length > 0);
});

test("12. validateFile 拒绝空文件名", () => {
  const result = validateFile("");
  assert.strictEqual(result.valid, false);
});
