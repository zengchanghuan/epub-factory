/**
 * 测试 F8：极简白 UI 设计要素
 * - F8-1 存在顶部品牌栏（.topbar）
 * - F8-2 主按钮使用深色（非蓝色）背景
 * - F8-3 进度条高度为 3px（细线设计）
 * - F8-4 引入 Inter 字体
 * - F8-5 CSS 变量 --primary 为深色（非 #2563eb）
 * - F8-6 移动端断点存在（max-width: 640px）
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F8-1 存在顶部品牌栏", () => {
  assert.ok(html.includes("topbar"), "应包含 .topbar 品牌栏");
  assert.ok(html.includes("topbar-logo"), "应包含 logo 元素");
  assert.ok(html.includes("FixEpub"), "品牌名应为 FixEpub");
});

test("F8-2 主按钮使用深色背景", () => {
  // --primary 不应再是蓝色 #2563eb
  assert.ok(!html.includes("--primary: #2563eb"), "主色不应为旧蓝色 #2563eb");
  assert.ok(html.includes("--primary: #0f0f0f"), "主色应为近黑色 #0f0f0f");
});

test("F8-3 进度条高度为细线 3px", () => {
  assert.ok(html.includes("height: 3px"), "进度条应为 3px 细线");
});

test("F8-4 引入 Inter 字体", () => {
  assert.ok(html.includes("Inter"), "应引入 Inter 字体");
});

test("F8-5 CSS 变量配置了极简色板", () => {
  assert.ok(html.includes("--surface:"), "应有 --surface 变量");
  assert.ok(html.includes("--subtle:"), "应有 --subtle 变量");
  assert.ok(html.includes("-webkit-font-smoothing: antialiased"), "应开启字体抗锯齿");
});

test("F8-6 移动端响应式断点存在", () => {
  assert.ok(html.includes("max-width: 640px"), "应有 640px 移动端断点");
  assert.ok(html.includes("max-width: 520px"), "应有 520px 表单断点");
});
