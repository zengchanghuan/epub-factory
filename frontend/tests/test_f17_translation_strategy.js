/**
 * F17: strategy field, resolved strategy display, and manual override.
 */

const { buildFormFields, formatJobMeta } = require("../lib");

test("1. 翻译表单默认提交 auto 策略", () => {
  const fields = buildFormFields({
    enableTranslation: true,
    targetLang: "zh-CN",
  });
  assert.strictEqual(fields.translation_strategy, "auto");
});

test("2. 翻译表单透传人工锁定策略", () => {
  const fields = buildFormFields({
    enableTranslation: true,
    translationStrategy: "mirror_fidelity",
  });
  assert.strictEqual(fields.translation_strategy, "mirror_fidelity");
});

test("3. 任务元信息优先显示探针解析后的策略", () => {
  const text = formatJobMeta({
    enable_translation: true,
    target_lang: "zh-CN",
    device: "generic",
    translation_strategy: "auto",
    translation_stats: {
      translation_strategy_resolved: "academic_rigorous",
    },
  });
  assert.ok(text.includes("学术严谨"), text);
});
