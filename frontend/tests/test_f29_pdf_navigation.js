const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
test('PDF 当前独立入口使用保留原文产品', () => {
  assert(html.includes('pdf-conversion.js'));
  assert(!html.includes('pdfServiceOptions'));
  assert(!html.includes('PDF 转 EPUB：OCR 与翻译'));
  assert(!html.includes('PDF 的 OCR、图文布局保留与转换验收'));
});
