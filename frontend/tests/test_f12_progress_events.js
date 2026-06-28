/**
 * F12 测试：任务进度日志消费结构化 events
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F12-1 主页面轮询 job events 并追加到进度日志", () => {
  assert.ok(
    html.includes("function pollJobEvents(jobId)"),
    "应有 pollJobEvents 负责读取结构化事件"
  );
  assert.ok(
    html.includes('/events`'),
    "应请求 /api/v2/jobs/{job_id}/events"
  );
  assert.ok(
    html.includes("appendProgressLog(message, key)"),
    "events 应带 key 追加，避免轮询重复刷日志"
  );
});

test("F12-2 新任务和任务详情会重置事件去重状态", () => {
  assert.ok(html.includes("let renderedEventKeys = new Set();"), "应维护事件去重集合");
  assert.ok(html.includes("function resetProgressLog()"), "应集中重置日志状态");
  assert.ok(html.includes("renderedEventKeys = new Set();"), "重置时应清空事件去重集合");
});
