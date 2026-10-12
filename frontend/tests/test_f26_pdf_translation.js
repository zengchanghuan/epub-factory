const pdf = require('../pdf-translation');
const caps = { enabled: true, max_file_mb: 300 };
const scan = { name: 'Book.PDF', size: 201 * 1024 * 1024 };
test('F26 PDF accepts large originals for fallback only in single translation flow', () => {
  assert.equal(pdf.validate([scan], 'translate', caps), '');
  assert.ok(pdf.validate([scan], 'convert', caps));
  assert.ok(pdf.validate([scan, {name:'book.epub'}], 'translate', caps));
});
test('F26 server readiness and network failures keep PDF closed', () => {
  assert.ok(pdf.validate([scan], 'translate', {enabled:false}));
  assert.ok(pdf.validate([scan], 'translate', null));
  assert.ok(pdf.validate([scan], 'translate', {enabled:'true', max_file_mb:300}));
});
test('F26 PDF size check does not alter existing EPUB selection', () => {
  assert.ok(pdf.validate([{...scan, size:301*1024*1024}], 'translate', caps));
  assert.equal(pdf.validate([{name:'book.epub',size:1024}], 'convert', {}), '');
});
test('F26 explicit price and unverified preview rather than OCR success claim', () => {
  const text = pdf.summary({kind:'mixed',page_count:201,ocr_pages:4,pricing:{price_cny:'40.20'}});
  for (const phrase of ['混合型','201 页','4 页需 OCR','40.20','尚未经过 OCR 验证','付款后','不重绘翻译','可靠文字直接复用','疑难页使用 OCR']) assert.ok(text.includes(phrase));
});
