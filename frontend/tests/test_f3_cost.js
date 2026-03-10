/**
 * F3 测试：翻译费用摘要
 *
 * 测试用例：
 * 1. formatTranslationCost 正常输出 tokens + cost
 * 2. totalTokens=0 显示 "0 tokens"
 * 3. costUsd=0 显示 "$0.0000"
 * 4. 大数字 tokens 有千分位分隔符
 * 5. costUsd 保留 4 位小数
 * 6. totalTokens 缺失时显示 "0"
 * 7. costUsd 缺失时显示 "$0.0000"
 */

const { formatTranslationCost } = require("../lib");

test("1. 正常输出格式", () => {
  const result = formatTranslationCost({ totalTokens: 12345, costUsd: 0.0134 });
  assert.ok(result.includes("12,345"), `应含 12,345，实际: ${result}`);
  assert.ok(result.includes("$0.0134"), `应含 $0.0134，实际: ${result}`);
});

test("2. totalTokens=0 → '0 tokens'", () => {
  const result = formatTranslationCost({ totalTokens: 0, costUsd: 0 });
  assert.ok(result.startsWith("0 tokens"), `实际: ${result}`);
});

test("3. costUsd=0 → '$0.0000'", () => {
  const result = formatTranslationCost({ totalTokens: 100, costUsd: 0 });
  assert.ok(result.includes("$0.0000"), `实际: ${result}`);
});

test("4. 大数字有千分位", () => {
  const result = formatTranslationCost({ totalTokens: 1000000, costUsd: 1.2345 });
  assert.ok(result.includes("1,000,000"), `应含千分位，实际: ${result}`);
});

test("5. costUsd 保留 4 位小数", () => {
  const result = formatTranslationCost({ totalTokens: 100, costUsd: 0.12345 });
  assert.ok(result.includes("$0.1235"), `四舍五入4位，实际: ${result}`);
});

test("6. totalTokens 缺失显示 '0 tokens'", () => {
  const result = formatTranslationCost({ costUsd: 0.01 });
  assert.ok(result.startsWith("0 tokens"), `实际: ${result}`);
});

test("7. costUsd 缺失显示 '$0.0000'", () => {
  const result = formatTranslationCost({ totalTokens: 500 });
  assert.ok(result.includes("$0.0000"), `实际: ${result}`);
});
