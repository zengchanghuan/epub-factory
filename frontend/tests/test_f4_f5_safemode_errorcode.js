/**
 * F4 + F5 测试：SafeMode 降级提示 & error_code 展示
 *
 * 测试用例：
 * 1.  isSafeMode("...safe...") → true
 * 2.  isSafeMode("...SafeMode...") → true
 * 3.  isSafeMode("...full...") → false
 * 4.  isSafeMode("") → false
 * 5.  formatErrorCode("CONVERT_FAILED") 含错误码
 * 6.  formatErrorCode("CONVERT_FAILED") 含说明文字
 * 7.  formatErrorCode("UNSUPPORTED_TYPE") 含对应说明
 * 8.  formatErrorCode("UNKNOWN_CODE") 只显示码，不崩溃
 * 9.  formatErrorCode(null) 返回空字符串
 * 10. formatErrorCode(undefined) 返回空字符串
 */

const { isSafeMode, formatErrorCode } = require("../lib");

// ─── isSafeMode ────────────────────────────────────────────────────────────

test("1. 含 'safe' → true", () => {
  assert.strictEqual(isSafeMode("Pipeline [safe] — 总耗时 350 ms"), true);
});

test("2. 含 'SafeMode' → true", () => {
  assert.strictEqual(isSafeMode("✅ SafeMode:Unpack   9.8 ms"), true);
});

test("3. 含 'full' → false", () => {
  assert.strictEqual(isSafeMode("Pipeline [full] — 总耗时 370 ms"), false);
});

test("4. 空字符串 → false", () => {
  assert.strictEqual(isSafeMode(""), false);
});

// ─── formatErrorCode ───────────────────────────────────────────────────────

test("5. CONVERT_FAILED 含错误码标识", () => {
  const result = formatErrorCode("CONVERT_FAILED");
  assert.ok(result.includes("CONVERT_FAILED"), `实际: ${result}`);
});

test("6. CONVERT_FAILED 含说明文字", () => {
  const result = formatErrorCode("CONVERT_FAILED");
  assert.ok(result.length > "CONVERT_FAILED".length + 2, `说明文字缺失: ${result}`);
});

test("7. UNSUPPORTED_TYPE 含对应说明", () => {
  const result = formatErrorCode("UNSUPPORTED_TYPE");
  assert.ok(result.includes("UNSUPPORTED_TYPE"), `实际: ${result}`);
  assert.ok(result.length > "UNSUPPORTED_TYPE".length + 2);
});

test("8. 未知错误码只显示码，不崩溃", () => {
  const result = formatErrorCode("SOME_RANDOM_CODE");
  assert.ok(result.includes("SOME_RANDOM_CODE"), `实际: ${result}`);
});

test("9. null 返回空字符串", () => {
  assert.strictEqual(formatErrorCode(null), "");
});

test("10. undefined 返回空字符串", () => {
  assert.strictEqual(formatErrorCode(undefined), "");
});
