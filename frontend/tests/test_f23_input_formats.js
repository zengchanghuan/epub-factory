const fs = require('fs');
const path = require('path');
const {validateFile} = require('../lib.js');

for (const page of ['index.html', 'epub-translator.html', 'vertical-to-horizontal.html', 'traditional-to-simplified.html']) {
  test(`PDF gate: ${page} disables upload and unsupported advertising`, () => {
    const html = fs.readFileSync(path.join(__dirname, '..', page), 'utf8');
    const accepts = [...html.matchAll(/accept="([^"]+)"/g)].map(match => match[1]);
    assert(accepts.length > 0);
    assert(accepts.every(value => !value.toLowerCase().includes('.pdf')));
    assert(html.includes('暂不支持 PDF'));
    assert(html.includes('lib.js?v=20260918-corpus-fixes'));
    assert(!html.includes('"PDF 转换"'));
    assert(!html.includes('FixEpub 支持将 PDF'));
    assert(!html.includes('一键上传 EPUB/PDF'));
    assert(!html.includes('AI 电子书翻译工具 (EPUB/PDF)'));
    for (const script of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)) {
      if (script[1].includes('application/ld+json')) JSON.parse(script[2]);
      else if (!script[1].includes('src=')) new Function(script[2]);
    }
  });
}

test('PDF gate: shared validator rejects upper/lower case before submission', () => {
  for (const name of ['book.pdf', 'book.PDF']) {
    const result = validateFile(name);
    assert.strictEqual(result.valid, false);
    assert(result.error.includes('暂不支持 PDF'));
  }
  assert.strictEqual(validateFile('book.epub').valid, true);
  const landing = fs.readFileSync(path.join(__dirname, '..', '..', 'index.html'), 'utf8');
  assert(landing.includes('PDF is not currently supported'));
  assert(!landing.includes('EPUB & PDF upload'));
});
