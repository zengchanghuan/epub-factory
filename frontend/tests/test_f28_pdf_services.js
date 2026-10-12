const pdfServices = require('../pdf-translation');
const available = {enabled:true, max_file_mb:300, services:{ocr:{enabled:true},translate:{enabled:true}}};
const bookPdf = {name:'中文书.pdf',size:1024};

test('F28 独立服务开放状态：仅 OCR 可单独上线', () => {
  const caps = {...available, services:{ocr:{enabled:true},translate:{enabled:false}}};
  assert.equal(pdfServices.validate([bookPdf], 'pdf', caps, 'ocr'), '');
  assert.ok(pdfServices.validate([bookPdf], 'pdf', caps, 'translate'));
  assert.ok(pdfServices.validate([{name:'book.epub'}], 'pdf', caps, 'ocr'));
  assert.ok(pdfServices.validate([bookPdf], 'pdf', {enabled:true,max_file_mb:300}, 'ocr'));
});

test('F28 仅 OCR 显示本服务价格、不暗示翻译或原页图片', () => {
  const detail = {service_mode:'ocr',include_source_pages:false,kind:'scan',page_count:20,ocr_pages:20,pricing:{price_cny:'0.99'}};
  const text = pdfServices.summary(detail);
  for(const expected of ['仅 OCR','¥0.99','保留原语言','不收翻译费用','不附原页图片'])assert.ok(text.includes(expected));
  assert.ok(!text.includes('付款后才开始识别和翻译'));
  const translated = pdfServices.summary({...detail,service_mode:'translate',pricing:{price_cny:'4.00'}});
  assert.ok(translated.includes('OCR＋翻译'));
  assert.ok(translated.includes('¥4.00'));
  assert.ok(translated.includes('不另加一笔 OCR 费用'));
});

test('F28 订单重载识别两种 PDF 产品，不误标为普通格式转换', () => {
  assert.equal(pdfServices.label({source_filename:'Book.PDF',enable_translation:false}), '仅 OCR');
  assert.equal(pdfServices.label({pdf_service:'translate',enable_translation:true}), 'OCR＋翻译');
  assert.equal(pdfServices.label({source_filename:'book.epub',enable_translation:false}), '转换');
});

test('旧 OCR helper 不接入当前主页', () => {
  const html = require('fs').readFileSync(require('path').join(__dirname,'../index.html'),'utf8');
  assert(!html.includes('pdf-translation.js'));
  assert(html.includes('pdf-conversion.js'));
  assert(!html.includes('/api/v2/pdf-capabilities'));
});
