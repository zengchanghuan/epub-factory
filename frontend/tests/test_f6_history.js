/**
 * F6 测试：LocalStorage 历史记录
 *
 * 测试用例：
 * 1.  buildHistoryEntry 包含必要字段
 * 2.  buildHistoryEntry downloadUrl 拼接正确
 * 3.  buildHistoryEntry download_url 为 null 时 downloadUrl 为 null
 * 4.  loadHistory 空 storage 返回空数组
 * 5.  loadHistory 损坏 JSON 返回空数组
 * 6.  saveHistory 保存一条记录后可取回
 * 7.  saveHistory 相同 jobId 覆盖而非追加
 * 8.  saveHistory 超过 HISTORY_MAX 时截断
 * 9.  最新记录在列表首位
 * 10. clearHistory 清除所有记录
 * 11. saveHistory 返回更新后的列表
 * 12. buildHistoryEntry savedAt 为 ISO 字符串
 */

const {
  buildHistoryEntry, loadHistory, saveHistory, clearHistory, HISTORY_MAX,
} = require("../lib");

// ─── 模拟 localStorage ────────────────────────────────────────────────────

function makeStorage() {
  const store = {};
  return {
    getItem: (k) => store[k] ?? null,
    setItem: (k, v) => { store[k] = v; },
    removeItem: (k) => { delete store[k]; },
  };
}

function makeJob(overrides = {}) {
  return {
    job_id: "abc123",
    trace_id: "trace001",
    source_filename: "book.epub",
    output_mode: "simplified",
    device: "generic",
    enable_translation: false,
    bilingual: false,
    download_url: "/api/v1/jobs/abc123/download",
    ...overrides,
  };
}

// ─── Tests ────────────────────────────────────────────────────────────────

test("1. buildHistoryEntry 包含 jobId / filename / meta / savedAt", () => {
  const entry = buildHistoryEntry(makeJob(), "http://localhost:8000");
  assert.ok("jobId" in entry);
  assert.ok("filename" in entry);
  assert.ok("meta" in entry);
  assert.ok("savedAt" in entry);
});

test("2. buildHistoryEntry downloadUrl 拼接正确", () => {
  const entry = buildHistoryEntry(makeJob(), "http://localhost:8000");
  assert.strictEqual(entry.downloadUrl, "http://localhost:8000/api/v1/jobs/abc123/download");
});

test("3. download_url 为 null 时 downloadUrl 为 null", () => {
  const entry = buildHistoryEntry(makeJob({ download_url: null }), "http://localhost:8000");
  assert.strictEqual(entry.downloadUrl, null);
});

test("4. loadHistory 空 storage 返回 []", () => {
  const list = loadHistory(makeStorage());
  assert.deepStrictEqual(list, []);
});

test("5. loadHistory 损坏 JSON 返回 []", () => {
  const s = makeStorage();
  s.setItem("epub_factory_history", "{ invalid json ]");
  const list = loadHistory(s);
  assert.deepStrictEqual(list, []);
});

test("6. saveHistory 保存后 loadHistory 可取回", () => {
  const s = makeStorage();
  const entry = buildHistoryEntry(makeJob(), "http://localhost:8000");
  saveHistory(s, entry);
  const list = loadHistory(s);
  assert.strictEqual(list.length, 1);
  assert.strictEqual(list[0].jobId, "abc123");
});

test("7. 相同 jobId 覆盖而非追加", () => {
  const s = makeStorage();
  const e1 = buildHistoryEntry(makeJob({ source_filename: "old.epub" }), "http://localhost:8000");
  const e2 = buildHistoryEntry(makeJob({ source_filename: "new.epub" }), "http://localhost:8000");
  saveHistory(s, e1);
  saveHistory(s, e2);
  const list = loadHistory(s);
  assert.strictEqual(list.length, 1);
  assert.strictEqual(list[0].filename, "new.epub");
});

test("8. 超过 HISTORY_MAX 时截断", () => {
  const s = makeStorage();
  for (let i = 0; i < HISTORY_MAX + 3; i++) {
    const entry = buildHistoryEntry(makeJob({ job_id: `job${i}`, source_filename: `book${i}.epub` }), "http://localhost:8000");
    saveHistory(s, entry);
  }
  const list = loadHistory(s);
  assert.strictEqual(list.length, HISTORY_MAX, `应截断至 ${HISTORY_MAX}，实际 ${list.length}`);
});

test("9. 最新记录在列表首位", () => {
  const s = makeStorage();
  const e1 = buildHistoryEntry(makeJob({ job_id: "first", source_filename: "first.epub" }), "http://localhost:8000");
  const e2 = buildHistoryEntry(makeJob({ job_id: "second", source_filename: "second.epub" }), "http://localhost:8000");
  saveHistory(s, e1);
  saveHistory(s, e2);
  const list = loadHistory(s);
  assert.strictEqual(list[0].jobId, "second");
});

test("10. clearHistory 后 loadHistory 返回 []", () => {
  const s = makeStorage();
  saveHistory(s, buildHistoryEntry(makeJob(), "http://localhost:8000"));
  clearHistory(s);
  assert.deepStrictEqual(loadHistory(s), []);
});

test("11. saveHistory 返回更新后的列表", () => {
  const s = makeStorage();
  const returned = saveHistory(s, buildHistoryEntry(makeJob(), "http://localhost:8000"));
  assert.ok(Array.isArray(returned));
  assert.strictEqual(returned.length, 1);
});

test("12. buildHistoryEntry savedAt 为合法 ISO 字符串", () => {
  const entry = buildHistoryEntry(makeJob(), "http://localhost:8000");
  assert.ok(!isNaN(new Date(entry.savedAt).getTime()), `savedAt 无效: ${entry.savedAt}`);
});
