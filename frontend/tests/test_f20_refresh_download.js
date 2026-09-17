const fs = require('fs');
const path = require('path');
const vm = require('vm');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const tokenSource = html.slice(html.indexOf('    const JOB_TOKEN_STORE_KEY'), html.indexOf('    function escapeHtml'));
const actionSource = html.slice(html.indexOf('    function handleTaskPrimaryAction'), html.indexOf('    function openJobDetail'));

test('F20-1 刷新后沿用原会话及任务访问权限', () => {
  const values = new Map([['anon_client_session_v1', 'original-session'], ['job_access_tokens_v1', JSON.stringify({ book: 'original-token' })]]);
  const localStorage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) };
  for (let reload = 0; reload < 2; reload++) {
    const context = { localStorage, crypto: { randomUUID() { throw new Error('Must retain session'); } } };
    vm.createContext(context); vm.runInContext(tokenSource, context);
    assert.equal(context.clientHeaders()['X-Client-Session'], 'original-session');
    assert.equal(context.authHeaders('book')['X-Job-Token'], 'original-token');
  }
});

test('F20-2 下载按钮先获取新链接，不使用本地过期链接', () => {
  const requests = [];
  const chain = value => ({ then: fn => chain(fn(value)), catch() { return this; } });
  const context = {
    API: '', window: { location: {} }, authHeaders: () => ({ 'X-Job-Token': 'original-token' }),
    withJobToken: url => url + '&token=original-token', openJobDetail() { throw new Error('Must download'); },
    fetch(url, options) { requests.push({ url, options }); return chain({ ok: true, json: () => ({ status: 'completed', download_url: '/fresh-download?sig=new' }) }); },
  };
  vm.createContext(context); vm.runInContext(actionSource, context); context.handleTaskPrimaryAction('book');
  assert.equal(requests[0].url, '/api/v2/jobs/book'); assert.equal(requests[0].options.cache, 'no-store');
  assert.equal(context.window.location.href, '/fresh-download?sig=new&token=original-token');
});

test('F20-3 首页引用带发布版本的前端逻辑', () => { assert.ok(/<script src="lib\.js\?v=[^"]+"/.test(html)); });
test('F20-4 保留报价与付款点击记录', () => {
  assert.ok(html.includes('/checkout-events')); assert.ok(html.includes('reportCheckoutEvent("quote_shown")'));
  assert.ok(html.includes('reportCheckoutEvent("payment_clicked")'));
});
