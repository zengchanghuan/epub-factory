/**
 * F11 测试：部分翻译失败不可下载
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F11-1 任务中心不把 partial_completed/qa_failed 当作直接下载状态", () => {
  assert.ok(
    !html.includes('data.status === "completed" || data.status === "partial_completed"'),
    "partial_completed 不应进入直接下载判断"
  );
  assert.ok(
    !html.includes('data.status === "completed" || data.status === "qa_failed"'),
    "qa_failed 不应进入直接下载判断"
  );
  assert.ok(
    html.includes('if (data.status === "completed" && data.download_url)'),
    "只有 completed 才能触发直接下载"
  );
});

test("F11-2 详情页将 partial_completed/qa_failed 当作失败态展示", () => {
  assert.ok(
    html.includes('function isTerminalFailureStatus(status)'),
    "应集中判断终态失败状态"
  );
  assert.ok(
    html.includes('status === "partial_completed" || status === "qa_failed"'),
    "partial_completed/qa_failed 应进入失败态分支"
  );
  assert.ok(
    html.includes('$("resultActions").classList.remove("visible")'),
    "partial_completed/failed 时应隐藏下载区域"
  );
});

test("F11-3 质检失败提供免费重译入口", () => {
  assert.ok(html.includes('id="qaReport"'), "应展示 QA 质检报告区域");
  assert.ok(html.includes('id="retryTranslationBtn"'), "应有免费重新翻译按钮");
  assert.ok(html.includes('/retry-translation'), "应调用免费重译接口");
});
