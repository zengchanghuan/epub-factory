/* PDF input is advertised only after the server confirms configuration readiness. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PdfTranslation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function validate(files, mode, capabilities, service = 'translate') {
    const pdfs = Array.from(files || []).filter(file => /\.pdf$/i.test(file.name));
    if (!pdfs.length) return mode === 'pdf' ? 'PDF 转 EPUB 仅支持 PDF 文件' : '';
    if (!capabilities || capabilities.enabled !== true) return 'PDF 服务正在准备中，暂未开放';
    if (!['pdf', 'translate'].includes(mode)) return '请使用顶部「PDF 转 EPUB」入口';
    if (!['ocr', 'translate'].includes(service)) return '请选择仅 OCR 或 OCR＋翻译';
    if (service === 'ocr' && capabilities.services?.ocr?.enabled !== true) return '仅 OCR 服务暂未开放';
    if (capabilities.services && capabilities.services[service]?.enabled !== true) return '所选 PDF 服务暂未开放';
    if (files.length !== 1) return 'PDF 请一次上传一个文件，暂不支持与其他书籍批量提交';
    if (pdfs[0].size > Number(capabilities.max_file_mb) * 1024 * 1024) return `PDF 最大支持 ${capabilities.max_file_mb}MB`;
    return '';
  }
  function summary(pdf) {
    const kinds = { text: '文本型', scan: '扫描型', mixed: '混合型' };
    const ocrOnly = pdf.service_mode === 'ocr';
    const label = ocrOnly ? '仅 OCR' : 'OCR＋翻译';
    const output = pdf.include_source_pages === false
      ? '\n下载可重排 EPUB，不附原页图片。'
      : '\n下载 EPUB，按原 PDF 页码对照；原页图表、公式保留为图片，图片内文字不重绘翻译。';
    return `${kinds[pdf.kind] || 'PDF'} · ${Number(pdf.page_count)} 页，其中 ${Number(pdf.ocr_pages)} 页需 OCR。` +
      '\n可靠文字直接复用，扫描及疑难页使用 OCR 结果。' +
      `\n服务：${label}。本次报价 ¥${pdf.pricing.price_cny}，按 PDF 总页数计费，确认后不追加收费。` +
      (ocrOnly ? '\n保留原语言，不翻译；本订单不收翻译费用。' : '\n报价包含 OCR 与翻译，不另加一笔 OCR 费用。') + output +
      `\n以下为原页预览，尚未经过 OCR 验证。付款后才开始${ocrOnly ? '识别' : '识别和翻译'}。` +
      '\n需要 OCR 时会向 MinerU 或 DeepSeek 发送原文件或页图。';
  }
  function serviceFor(job) {
    return job.pdf_service || job.translation_preflight?.pdf?.service_mode ||
      (/\.pdf$/i.test(job.source_filename || '') ? (job.enable_translation ? 'translate' : 'ocr') : '');
  }
  function label(job) {
    const service = serviceFor(job);
    return service ? (service === 'ocr' ? '仅 OCR' : 'OCR＋翻译') : (job.enable_translation ? '翻译' : '转换');
  }
  return { validate, summary, serviceFor, label };
});
