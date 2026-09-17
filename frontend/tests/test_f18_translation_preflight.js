/**
 * F18 测试：翻译支付前画像确认与在线设定修订。
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "../index.html"),
  "utf-8"
);

test("F18-1 翻译上传明确请求支付前画像确认", () => {
  assert.ok(
    html.includes('form.append("profile_confirmation", "true")'),
    "翻译上传必须请求两阶段确认"
  );
  assert.ok(
    html.includes('data.status === "awaiting_confirmation"'),
    "前端必须识别待确认状态"
  );
});

test("F18-2 确认面板可修订术语角色和章节策略", () => {
  assert.ok(html.includes('id="translationPreflightPanel"'), "应有画像确认面板");
  assert.ok(html.includes('id="preflightGlossary"'), "应有术语编辑器");
  assert.ok(html.includes('id="preflightCharacters"'), "应有角色编辑器");
  assert.ok(html.includes("chapter-strategy-override"), "应有章节策略覆盖控件");
});

test("F18-3 用户确认后才调用创建支付订单接口", () => {
  assert.ok(
    html.includes("/confirm-profile"),
    "确认按钮应调用画像确认接口"
  );
  assert.ok(
    html.includes("确认并生成支付订单"),
    "按钮文案应明确确认后才生成订单"
  );
  assert.ok(
    html.includes("enable_term_highlights"),
    "应支持显式开启确定性术语标记"
  );
});
