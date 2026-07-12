/**
 * 测试 F16：完整批量转换入口、统一支付、批次进度与 ZIP 下载。
 */

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.resolve(__dirname, "../index.html"), "utf-8");

test("F16-1 文件选择器支持多选并限制批次数量", () => {
  assert.ok(html.includes('id="fileInput" type="file" multiple'), "文件选择器应支持 multiple");
  assert.ok(html.includes("if (nextFiles.length > 10)"), "前端应限制单批最多 10 个文件");
  assert.ok(html.includes("selectedFiles = nextFiles"), "应保存完整文件列表");
});

test("F16-1b 支持递归选择整个文件夹", () => {
  assert.ok(html.includes('id="folderInput" type="file" multiple webkitdirectory directory'), "应有文件夹选择器");
  assert.ok(html.includes('id="folderSelectBtn"'), "应有选择整个文件夹按钮");
  assert.ok(html.includes("selectFiles(folderInput.files, true)"), "文件夹内容应进入批量选择流程");
  assert.ok(html.includes("const supportedFiles = allFiles.filter"), "应忽略文件夹内不支持的文件");
  assert.ok(html.includes('selectedFile.webkitRelativePath.split("/")[0]'), "应显示所选文件夹名称");
});

test("F16-2 多文件提交到批次 API", () => {
  assert.ok(html.includes('selectedFiles.forEach(file => form.append("files", file))'), "应逐个追加 files 字段");
  assert.ok(html.includes('`${API}/api/v2/batches`'), "多文件应调用批次 API");
  assert.ok(html.includes("批量模式当前仅支持转换与排版处理"), "批量模式应禁用 AI 翻译");
});

test("F16-3 批次共享 token 并轮询汇总进度", () => {
  assert.ok(html.includes('saveJobToken(`batch:${data.batch_id}`, data.access_token)'), "应保存批次访问 token");
  assert.ok(html.includes("function pollBatchV2(batchId)"), "应实现批次轮询");
  assert.ok(html.includes('`${API}/api/v2/batches/${batchId}`'), "应查询批次详情");
  assert.ok(html.includes("data.progress_percent"), "应显示聚合进度");
});

test("F16-4 支持支付回跳恢复与 ZIP 下载", () => {
  assert.ok(html.includes('params.get("batch_id")'), "应识别支付宝批次回跳参数");
  assert.ok(html.includes('`${API}/api/v2/batches/${batchId}/recover`'), "回跳后应主动查单恢复");
  assert.ok(html.includes("↓ 下载批次 ZIP"), "应提供批次 ZIP 下载入口");
});
