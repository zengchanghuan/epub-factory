const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");

function assertTranslationGuidance(text) {
  assert.ok(text.includes("双语"), "应说明双语对照能力");
  assert.ok(text.includes("专业术语和重要内容建议结合原文复核"), "应保留原文复核提醒");
  assert.ok(!/文学级翻译|深度理解语义|不同于传统的机翻/.test(text), "不应恢复过度承诺的翻译文案");
}

test("首页翻译 FAQ 结构化数据保留能力说明与原文复核提醒", () => {
  const schemas = Array.from(html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g),
    match => JSON.parse(match[1]));
  const faq = schemas.find(schema => schema["@type"] === "FAQPage");
  assert.ok(faq, "首页 FAQ 必须是有效 JSON-LD");
  const answer = faq.mainEntity.find(item => item.name.includes("翻译质量"));
  assert.ok(answer, "应保留翻译问答");
  assertTranslationGuidance(answer.acceptedAnswer.text);
});

test("首页可见翻译介绍保留辅助阅读定位与原文复核提醒", () => {
  const cards = Array.from(html.matchAll(/<article class="feature-card">([\s\S]*?)<\/article>/g), match => match[1]);
  const card = cards.find(content => /<h3>[\s\S]*(?:翻译|英译中)[\s\S]*<\/h3>/.test(content));
  assert.ok(card, "应保留可见的翻译介绍卡片");
  assertTranslationGuidance(card);
  assert.ok(card.includes("辅助阅读"), "应说明译文适用范围");
  assert.ok(card.includes("尽量保留目录、插图和可重排布局"));
});
