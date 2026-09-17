/**
 * 极简测试 runner（零依赖，纯 Node.js）
 * 用法：node runner.js test_f1_bilingual.js
 */
const assert = require("assert");

let passed = 0;
let failed = 0;
const failures = [];
const tests = [];

global.test = function (name, fn) {
  tests.push({ name, fn });
};

async function run() {
  for (const { name, fn } of tests) {
    try {
      await fn();
      console.log(`  ✅ ${name}`);
      passed++;
    } catch (e) {
      console.log(`  ❌ ${name}`);
      console.log(`     → ${e.message}`);
      failures.push({ name, error: e.message });
      failed++;
    }
  }
  console.log(`\n${"─".repeat(52)}`);
  console.log(`📊 Results: ${passed} passed, ${failed} failed`);
  console.log(`${"─".repeat(52)}`);
  process.exit(failed > 0 ? 1 : 0);
}

global.assert = assert;

const file = process.argv[2];
if (!file) {
  console.error("Usage: node runner.js <test-file>");
  process.exit(1);
}

const path = require("path");
require(path.resolve(__dirname, file));

run().catch(error => { console.error(error); process.exit(1); });
