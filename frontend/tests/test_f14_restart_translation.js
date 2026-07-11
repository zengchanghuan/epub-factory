/**
 * F14 测试：翻译任务可主动重启
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F14-1 主页面提供终态重启翻译按钮", () => {
  assert.ok(html.includes('id="restartActions"'), "应有重启操作区");
  assert.ok(html.includes('id="restartTranslationBtn"'), "应有重启翻译按钮");
  assert.ok(html.includes("重启翻译"), "按钮文案应直接说明重启翻译");
});

test("F14-2 仅取消或失败的翻译任务显示重启按钮", () => {
  assert.ok(
    html.includes("function canRestartTranslationStatus(status)"),
    "应集中判断可重启状态"
  );
  assert.ok(
    html.includes('status === "cancelled" || status === "failed"'),
    "cancelled/failed 应允许重启"
  );
  assert.ok(
    html.includes("!!data.enable_translation && canRestartTranslationStatus(s)"),
    "应只对翻译任务显示重启按钮"
  );
});

test("F14-3 点击重启会调用 restart 接口并写入 UI 日志", () => {
  assert.ok(html.includes("function restartTranslation(jobId)"), "应有 restartTranslation 处理点击");
  assert.ok(html.includes("/restart-translation`"), "应请求 /api/v2/jobs/{job_id}/restart-translation");
  assert.ok(html.includes("restartRequestedJobId"), "重启请求中不应被轮询重新启用按钮");
  assert.ok(html.includes("已请求重启翻译，正在重新加入队列"), "UI 日志应提示重启请求");
});

test("F14-4 运行中不应重复发送重启请求并应清空旧日志", () => {
  assert.ok(html.includes("let currentJobStatus"), "应记录当前任务状态");
  assert.ok(
    html.includes('currentJobStatus === "queued" || currentJobStatus === "running" || currentJobStatus === "pending_payment"'),
    "运行中、排队中、待支付状态应被前端拦截"
  );
  assert.ok(
    html.includes("任务仍在处理中；如需重启，请先停止当前翻译。"),
    "运行中重启提示应说明先停止当前翻译"
  );
  assert.ok(html.includes("resetProgressLog();"), "重启成功后应清空上一轮日志");
});
