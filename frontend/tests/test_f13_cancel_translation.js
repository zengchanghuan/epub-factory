/**
 * F13 测试：翻译任务可主动停止
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F13-1 主页面提供运行中停止翻译按钮", () => {
  assert.ok(html.includes('id="runningActions"'), "应有运行中操作区");
  assert.ok(html.includes('id="cancelJobBtn"'), "应有停止翻译按钮");
  assert.ok(html.includes("停止翻译"), "按钮文案应直接说明停止翻译");
  assert.ok(html.includes("stop-btn"), "停止按钮应有醒目的独立样式");
});

test("F13-2 仅翻译任务 queued/running 时显示停止按钮", () => {
  assert.ok(
    html.includes("function setRunningActionsVisible(visible, jobId)"),
    "应集中控制运行中操作区显示"
  );
  assert.ok(
    html.includes('!!data.enable_translation && (s === "queued" || s === "running")'),
    "应只在翻译任务 queued/running 时显示"
  );
});

test("F13-3 点击停止会调用 cancel 接口并保持停止中状态", () => {
  assert.ok(html.includes("function cancelJob(jobId)"), "应有 cancelJob 处理点击");
  assert.ok(html.includes('/cancel`'), "应请求 /api/v2/jobs/{job_id}/cancel");
  assert.ok(html.includes("cancelRequestedJobId"), "停止请求中不应被轮询重新启用按钮");
  assert.ok(html.includes("已请求停止翻译，正在停止新的模型请求"), "UI 日志应提示停止请求");
});
