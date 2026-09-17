const fs = require('fs');
const path = require('path');
const vm = require('vm');
const html = fs.readFileSync(path.resolve(__dirname, '../index.html'), 'utf8');
const source = html.slice(html.indexOf('    function renderQaReport(data)'), html.indexOf('    function retryTranslation(jobId)'));
function render(data) {
  const elements = new Map();
  function $(id) { if (!elements.has(id)) elements.set(id, { classList: { add() {}, remove() {} }, dataset: {} }); return elements.get(id); }
  const context = { $, qaReport: $('qaReport'), retryTranslationBtn: $('retryTranslationBtn'),
    translationDiagnosticsBtn: $('translationDiagnosticsBtn'), currentJobId: 'book',
    hideQaReport() { context.hidden = true; }, escapeHtml: text => String(text) };
  vm.createContext(context); vm.runInContext(source, context); context.renderQaReport(data);
  return { elements, context };
}
test('F21-1 可交付告警不显示失败或诱导整书重译', () => {
  const { elements, context } = render({ status: 'completed', qa_report: { status: 'warning', summary: '3 个段落建议复核', review_chunks: 3, checks: [] } });
  assert.ok(!context.hidden);
  assert.equal(elements.get('qaTitle').textContent, '译本可下载，建议复核');
  assert.ok(elements.get('qaSummary').textContent.includes('不等同于'));
  assert.equal(elements.get('retryTranslationBtn').hidden, true);
  assert.ok(elements.get('qaMeta').textContent.includes('分类可能重叠'));
});
test('F21-2 真正失败仍允许用户恢复，不继承告警隐藏状态', () => {
  const { elements } = render({ status: 'qa_failed', qa_report: { status: 'failed', retryable: true, checks: [] } });
  assert.equal(elements.get('qaTitle').textContent, '翻译质检未通过');
  assert.equal(elements.get('retryTranslationBtn').hidden, false);
  assert.equal(elements.get('retryTranslationBtn').disabled, false);
});
