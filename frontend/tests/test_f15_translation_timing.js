/**
 * F15 测试：翻译耗时归因面板
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F15-1 主页面提供翻译耗时归因面板", () => {
  assert.ok(html.includes('id="translationTimingCard"'), "应有翻译耗时归因面板");
  assert.ok(html.includes("翻译耗时归因"), "面板标题应可见");
  assert.ok(html.includes('id="translationTimingVerdict"'), "应展示主要瓶颈判断");
  assert.ok(html.includes('id="translationTimingStages"'), "应展示阶段耗时排行");
});

test("F15-2 详情轮询会消费后端 translation_timing 字段", () => {
  assert.ok(html.includes("function renderTranslationTiming(report)"), "应有集中渲染函数");
  assert.ok(html.includes("data.translation_timing"), "应从任务详情读取 translation_timing");
  assert.ok(html.includes("model_share"), "应展示模型耗时占比");
  assert.ok(html.includes("failure_categories"), "应展示失败类别");
});

test("F15-3 状态重置时会隐藏归因面板", () => {
  assert.ok(html.includes("function hideTranslationTiming()"), "应有隐藏函数");
  assert.ok(html.includes("hideTranslationTiming();"), "状态重置时应隐藏面板");
});
