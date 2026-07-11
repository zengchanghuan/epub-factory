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
  assert.ok(html.includes('id="copyTranslationTimingLogBtn"'), "应提供复制归因日志按钮");
  assert.ok(html.includes("复制日志"), "复制按钮文案应可见");
  assert.ok(html.includes('id="translationTimingVerdict"'), "应展示主要瓶颈判断");
  assert.ok(html.includes('id="translationTimingStages"'), "应展示阶段耗时排行");
  assert.ok(html.includes('id="translationFailedLocationTitle"'), "应提供失败段落定位标题");
  assert.ok(html.includes('id="translationTimingFailedLocations"'), "应提供失败段落定位列表");
});

test("F15-2 详情轮询会消费后端 translation_timing 字段", () => {
  assert.ok(html.includes("function renderTranslationTiming(report)"), "应有集中渲染函数");
  assert.ok(html.includes("data.translation_timing"), "应从任务详情读取 translation_timing");
  assert.ok(html.includes("model_share"), "应展示模型耗时占比");
  assert.ok(html.includes("failure_categories"), "应展示失败类别");
  assert.ok(html.includes("failed_chunk_locations"), "应展示失败段落定位");
});

test("F15-3 归因面板支持复制结构化日志", () => {
  assert.ok(html.includes("function formatTranslationTimingLog(report)"), "应格式化可复制日志");
  assert.ok(html.includes("log:'翻译耗时归因快照'"), "复制内容应包含稳定日志标题");
  assert.ok(html.includes("[elapsed_breakdown]"), "复制内容应拆分墙钟/归因/未归因耗时");
  assert.ok(html.includes("attributed_processing="), "复制内容应包含已归因处理耗时");
  assert.ok(html.includes("unattributed_wait_or_gap="), "复制内容应包含未归因等待或缺口");
  assert.ok(html.includes("attribution_coverage="), "复制内容应包含归因覆盖率");
  assert.ok(html.includes("model_stage="), "复制内容应包含模型耗时字段");
  assert.ok(html.includes("server_stage="), "复制内容应包含服务器耗时字段");
  assert.ok(html.includes("api_calls_estimated"), "应标注 API 调用是否估算");
  assert.ok(html.includes("api_calls_source"), "应标注 API 调用数据来源");
  assert.ok(html.includes("live_stats_unavailable"), "缺少实时 stats 时应避免冒充 API 次数");
  assert.ok(html.includes("api_latency_samples="), "复制内容应包含 API 延迟样本数");
  assert.ok(html.includes("chunk_latency_samples="), "复制内容应包含 chunk 延迟样本数");
  assert.ok(html.includes("chunk_latency_sum="), "复制内容应包含 chunk 延迟累计");
  assert.ok(html.includes("optimizations=complex_chunks:"), "复制内容应包含翻译优化计数");
  assert.ok(html.includes("inline_tag_repairs"), "复制内容应包含标签自动修复次数");
  assert.ok(html.includes("image_note_skipped"), "复制内容应包含图片注释跳过计数");
  assert.ok(html.includes("[failure_categories]"), "复制内容应包含失败类别段落");
  assert.ok(html.includes("[failed_chunk_locations]"), "复制内容应包含反复失败段落位置");
  assert.ok(html.includes("function formatChunkLocation"), "应格式化失败段落章节和页码");
  assert.ok(html.includes("function formatHumanChunkLocation"), "UI 应优先格式化人可读定位");
  assert.ok(html.includes("打开原 EPUB 到第"), "UI 应给出翻书定位提示");
  assert.ok(html.includes("原文片段："), "UI 应展示可搜索的原文片段");
  assert.ok(html.includes("技术定位"), "内部文件和 locator 应折叠到技术定位");
  assert.ok(html.includes("source_chapter_title"), "普通失败段落应支持源章节标题");
  assert.ok(html.includes("source_page"), "普通失败段落应支持源页码");
  assert.ok(html.includes("navigator.clipboard.writeText"), "应优先使用 Clipboard API");
  assert.ok(html.includes("document.execCommand(\"copy\")"), "应提供复制 fallback");
  assert.ok(html.includes("已复制"), "复制成功后应有 UI 状态");
  assert.ok(html.includes("复制失败"), "复制失败后应有 UI 状态");
});

test("F15-4 状态重置时会隐藏归因面板", () => {
  assert.ok(html.includes("function hideTranslationTiming()"), "应有隐藏函数");
  assert.ok(html.includes("hideTranslationTiming();"), "状态重置时应隐藏面板");
  assert.ok(html.includes("lastTranslationTimingLog = \"\""), "隐藏时应清理上一次复制日志");
});
