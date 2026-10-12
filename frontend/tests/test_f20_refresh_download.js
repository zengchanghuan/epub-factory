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
    const context = { window: {}, localStorage, crypto: { randomUUID() { throw new Error('Must retain session'); } } };
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

for (const batch of [false, true]) {
  test(`F20-5 重复${batch ? '批次' : '任务'}先恢复访问凭证再打开详情`, () => {
    const values = new Map();
    const opened = [];
    const context = {
      window: {}, localStorage: { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) },
      alert() { throw new Error('Valid recovery should open the original task'); },
      openJobDetail(id) { assert.equal(context.authHeaders(id)['X-Job-Token'], 'recovered'); opened.push(['job', id]); },
      openBatchDetail(id) { assert.equal(context.getJobToken(`batch:${id}`), 'recovered'); opened.push(['batch', id]); },
      loadTaskList() { opened.push(['refresh']); },
    };
    vm.createContext(context); vm.runInContext(tokenSource, context);
    context.openDuplicateUpload({ job_id: 'book', batch_id: batch ? 'bundle' : null, access_token: 'recovered' });
    assert.equal(context.getJobToken('book'), 'recovered');
    assert.deepStrictEqual(opened, [[batch ? 'batch' : 'job', batch ? 'bundle' : 'book'], ['refresh']]);
  });
}

test('F20-6 无权恢复或凭证过期时停留提示，不进入 403 轮询', () => {
  const alerts = [];
  const context = {
    alert: message => alerts.push(message),
    saveJobToken() { throw new Error('Must not save missing credentials'); },
    openJobDetail() { throw new Error('Must not poll inaccessible job'); },
    openBatchDetail() { throw new Error('Must not poll inaccessible batch'); },
    loadTaskList() { throw new Error('Must not open recovery UI'); },
  };
  const source = html.slice(html.indexOf('    function openDuplicateUpload'), html.indexOf('    function authHeaders'));
  vm.createContext(context); vm.runInContext(source, context);
  context.openDuplicateUpload({ job_id: 'book', batch_id: 'bundle', message: '请使用原任务链接或联系客服' });
  assert.deepStrictEqual(alerts, ['请使用原任务链接或联系客服']);
});

test('F20-7 创建和恢复任务携带当前账号凭证，退出登录后不沿用旧凭证', () => {
  const context = { window: {}, getClientSession: () => 'browser' };
  const source = html.slice(html.indexOf('    function clientHeaders'), html.indexOf('    function readJobTokens'));
  vm.createContext(context); vm.runInContext(source, context);
  assert.equal(context.clientHeaders().Authorization, undefined);
  let token = 'account-token';
  context.window.fixepubAuth = { getToken: () => token };
  assert.equal(context.clientHeaders().Authorization, 'Bearer account-token');
  assert.equal(context.clientHeaders()['X-Client-Session'], 'browser');
  token = '';
  assert.equal(context.clientHeaders().Authorization, undefined);
});
