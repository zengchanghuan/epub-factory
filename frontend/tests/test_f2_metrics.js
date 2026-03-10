/**
 * F2 测试：Pipeline 耗时折叠面板
 *
 * 测试用例：
 * 1. parseMetricsSummary 解析出阶段列表
 * 2. 每个阶段含 name / ms / status 字段
 * 3. 阶段 status 正确映射（✅=ok, ⚠️=warn, ❌=error）
 * 4. parseTotalMs 提取总耗时
 * 5. formatDuration < 1000ms 显示 ms
 * 6. formatDuration >= 1000ms 显示 s
 * 7. 空 summary 返回空数组
 * 8. parseTotalMs 无匹配返回 null
 * 9. 多阶段 summary 全部解析
 */

const { parseMetricsSummary, parseTotalMs, formatDuration } = require("../lib");

const SAMPLE_SUMMARY = `
────────────────────────────────────────────────────
⏱  Pipeline [full] — 总耗时 369 ms
────────────────────────────────────────────────────
  ✅ Unpack                          15.7 ms
  ✅   Cleaner:CjkNormalizer         43.4 ms
  ✅   Cleaner:CssSanitizer           0.2 ms
  ⚠️   Cleaner:TypographyEnhancer     1.6 ms
  ❌   Cleaner:StemGuard              0.0 ms
  ✅ TocRebuilder                     8.4 ms
  ✅ Packager+PostFix               297.5 ms
  ✅ EpubCheck                        0.1 ms
────────────────────────────────────────────────────
`.trim();

test("1. parseMetricsSummary 返回非空数组", () => {
  const stages = parseMetricsSummary(SAMPLE_SUMMARY);
  assert.ok(stages.length > 0, `应解析到阶段，实际: ${stages.length}`);
});

test("2. 每个阶段含 name / ms / status 字段", () => {
  const stages = parseMetricsSummary(SAMPLE_SUMMARY);
  for (const s of stages) {
    assert.ok("name" in s, "缺少 name");
    assert.ok("ms" in s, "缺少 ms");
    assert.ok("status" in s, "缺少 status");
  }
});

test("3a. ✅ 映射为 ok", () => {
  const stages = parseMetricsSummary(SAMPLE_SUMMARY);
  const unpack = stages.find(s => s.name.includes("Unpack"));
  assert.ok(unpack, "应有 Unpack 阶段");
  assert.strictEqual(unpack.status, "ok");
});

test("3b. ⚠️ 映射为 warn", () => {
  const stages = parseMetricsSummary(SAMPLE_SUMMARY);
  const typo = stages.find(s => s.name.includes("TypographyEnhancer"));
  assert.ok(typo, "应有 TypographyEnhancer 阶段");
  assert.strictEqual(typo.status, "warn");
});

test("3c. ❌ 映射为 error", () => {
  const stages = parseMetricsSummary(SAMPLE_SUMMARY);
  const stem = stages.find(s => s.name.includes("StemGuard"));
  assert.ok(stem, "应有 StemGuard 阶段");
  assert.strictEqual(stem.status, "error");
});

test("4. parseTotalMs 提取 369", () => {
  const total = parseTotalMs(SAMPLE_SUMMARY);
  assert.strictEqual(total, 369);
});

test("5. formatDuration(850) → '850 ms'", () => {
  assert.strictEqual(formatDuration(850), "850 ms");
});

test("6. formatDuration(1500) → '1.5 s'", () => {
  assert.strictEqual(formatDuration(1500), "1.5 s");
});

test("7. 空字符串 → 空数组", () => {
  const stages = parseMetricsSummary("");
  assert.strictEqual(stages.length, 0);
});

test("8. parseTotalMs 无匹配返回 null", () => {
  const total = parseTotalMs("no timing here");
  assert.strictEqual(total, null);
});

test("9. 多阶段全部解析（应为8个）", () => {
  const stages = parseMetricsSummary(SAMPLE_SUMMARY);
  assert.strictEqual(stages.length, 8, `期望8个，实际${stages.length}`);
});

test("10. ms 值正确解析（Packager+PostFix = 297.5）", () => {
  const stages = parseMetricsSummary(SAMPLE_SUMMARY);
  const pkg = stages.find(s => s.name.includes("Packager"));
  assert.ok(pkg, "应有 Packager 阶段");
  assert.strictEqual(pkg.ms, 297.5);
});
