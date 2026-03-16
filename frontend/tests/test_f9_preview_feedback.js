/**
 * 测试 F9：在线预览 + 反馈功能
 * - F9-1 HTML 包含预览 Modal（#previewModal）
 * - F9-2 HTML 包含反馈表单（.feedback-bar）
 * - F9-3 HTML 包含反馈类型按钮（至少 3 种）
 * - F9-4 HTML 包含 epub.js 懒加载逻辑（loadEpubJs）
 * - F9-5 HTML 包含键盘翻页逻辑（ArrowLeft/ArrowRight）
 * - F9-6 HTML 包含反馈提交到 /api/v2/feedback 的请求
 * - F9-7 HTML 中结果操作区（#resultActions）包含预览和下载按钮
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F9-1 存在预览 Modal", () => {
  assert.ok(html.includes('id="previewModal"'), "应有 #previewModal");
  assert.ok(html.includes("preview-topbar"), "应有顶部导航栏");
  assert.ok(html.includes("epub-viewer"), "应有 epub 渲染容器");
  assert.ok(html.includes("previewLoading"), "应有加载状态");
});

test("F9-2 存在反馈表单", () => {
  assert.ok(html.includes("feedback-bar"), "应有 .feedback-bar");
  assert.ok(html.includes("feedbackText"), "应有反馈文本区域");
  assert.ok(html.includes("submitFeedback"), "应有提交按钮");
});

test("F9-3 反馈类型按钮至少 3 种", () => {
  const matches = html.match(/class="fb-type/g);
  assert.ok(matches && matches.length >= 3, `反馈类型按钮应 ≥3，实际 ${matches ? matches.length : 0}`);
});

test("F9-4 epub.js 懒加载逻辑", () => {
  assert.ok(html.includes("loadEpubJs"), "应包含 loadEpubJs 函数");
  assert.ok(html.includes("epubjs"), "应引用 epub.js CDN");
  assert.ok(html.includes("ePub("), "应使用 ePub() 构造函数");
});

test("F9-5 键盘翻页支持", () => {
  assert.ok(html.includes("ArrowLeft"), "应支持左箭头翻页");
  assert.ok(html.includes("ArrowRight"), "应支持右箭头翻页");
  assert.ok(html.includes("Escape"), "应支持 Esc 关闭");
});

test("F9-6 反馈提交到正确 API 端点", () => {
  assert.ok(html.includes("/api/v2/feedback"), "应调用 /api/v2/feedback");
  assert.ok(html.includes('"Content-Type": "application/json"'), "应为 JSON 请求");
});

test("F9-7 结果操作区包含下载和预览按钮", () => {
  assert.ok(html.includes('id="resultActions"'), "应有 #resultActions 容器");
  assert.ok(html.includes('id="previewBtn"'), "应有预览按钮");
  assert.ok(html.includes('id="downloadBtn"'), "应有下载按钮");
  assert.ok(html.includes("showResultActions"), "应有 showResultActions 函数");
});
