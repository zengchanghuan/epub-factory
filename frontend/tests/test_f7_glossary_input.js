/**
 * F7 测试：术语表输入框逻辑
 *
 * 测试用例：
 * 1.  parseGlossaryInput 解析 = 分隔符
 * 2.  parseGlossaryInput 解析 : 分隔符
 * 3.  空行自动跳过
 * 4.  首选更靠前的分隔符（避免译文含分隔符时截断错误）
 * 5.  两侧空白自动 trim
 * 6.  格式错误行记录到 errors 数组
 * 7.  原文为空记录错误
 * 8.  译文为空记录错误
 * 9.  多条术语全部解析
 * 10. 空字符串返回空 glossary + 无 errors
 * 11. glossaryToJson 正常内容返回 JSON 字符串
 * 12. glossaryToJson 空内容返回 null
 * 13. glossaryToJson 解析结果可被 JSON.parse 还原
 * 14. buildFormFields 含 glossary_json 时透传到字段
 */

const { parseGlossaryInput, glossaryToJson, buildFormFields } = require("../lib");

test("1. = 分隔符正常解析", () => {
  const { glossary, errors } = parseGlossaryInput("Harry Potter = 哈利·波特");
  assert.strictEqual(glossary["Harry Potter"], "哈利·波特");
  assert.strictEqual(errors.length, 0);
});

test("2. : 分隔符正常解析", () => {
  const { glossary, errors } = parseGlossaryInput("Voldemort: 伏地魔");
  assert.strictEqual(glossary["Voldemort"], "伏地魔");
  assert.strictEqual(errors.length, 0);
});

test("3. 空行跳过", () => {
  const { glossary } = parseGlossaryInput("\n\nHarry = 哈利\n\n");
  assert.strictEqual(Object.keys(glossary).length, 1);
});

test("4. 译文含分隔符时取第一个 = 切分", () => {
  // "key = a=b" 应解析为 { key: "a=b" }
  const { glossary } = parseGlossaryInput("key = a=b");
  assert.strictEqual(glossary["key"], "a=b");
});

test("5. 两侧空白 trim", () => {
  const { glossary } = parseGlossaryInput("  spell  =  咒语  ");
  assert.ok("spell" in glossary, `key 应为 spell，实际: ${JSON.stringify(glossary)}`);
  assert.strictEqual(glossary["spell"], "咒语");
});

test("6. 无分隔符行记录 error", () => {
  const { errors } = parseGlossaryInput("这是一行错误");
  assert.strictEqual(errors.length, 1);
  assert.ok(errors[0].includes("格式错误"));
});

test("7. 原文为空记录 error", () => {
  const { errors } = parseGlossaryInput("= 译文");
  assert.strictEqual(errors.length, 1);
  assert.ok(errors[0].includes("原文为空"));
});

test("8. 译文为空记录 error", () => {
  const { errors } = parseGlossaryInput("原文 =");
  assert.strictEqual(errors.length, 1);
  assert.ok(errors[0].includes("译文为空"));
});

test("9. 多条术语全部解析", () => {
  const input = "Harry = 哈利\nHermione = 赫敏\nRon = 罗恩";
  const { glossary, errors } = parseGlossaryInput(input);
  assert.strictEqual(Object.keys(glossary).length, 3);
  assert.strictEqual(errors.length, 0);
  assert.strictEqual(glossary["Hermione"], "赫敏");
});

test("10. 空字符串返回空 glossary 无 errors", () => {
  const { glossary, errors } = parseGlossaryInput("");
  assert.strictEqual(Object.keys(glossary).length, 0);
  assert.strictEqual(errors.length, 0);
});

test("11. glossaryToJson 返回 JSON 字符串", () => {
  const result = glossaryToJson("AI = 人工智能");
  assert.ok(typeof result === "string");
  assert.ok(result.includes("AI"));
});

test("12. glossaryToJson 空内容返回 null", () => {
  assert.strictEqual(glossaryToJson(""), null);
  assert.strictEqual(glossaryToJson("   "), null);
  assert.strictEqual(glossaryToJson(null), null);
});

test("13. glossaryToJson 结果可被 JSON.parse 还原", () => {
  const json = glossaryToJson("wizard = 巫师\nspell = 咒语");
  const parsed = JSON.parse(json);
  assert.strictEqual(parsed["wizard"], "巫师");
  assert.strictEqual(parsed["spell"], "咒语");
});

test("14. buildFormFields 含 glossary_json 时包含该字段", () => {
  const fields = buildFormFields({
    outputMode: "simplified",
    device: "generic",
    enableTranslation: true,
    targetLang: "zh-CN",
    bilingual: false,
    glossaryJson: '{"AI":"人工智能"}',
  });
  assert.strictEqual(fields.glossary_json, '{"AI":"人工智能"}');
});
