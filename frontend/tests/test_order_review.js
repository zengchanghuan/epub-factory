/** Execute the complete admin controller against a small deterministic DOM. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'orders-admin.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'orders-admin.html'), 'utf8');
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const event = () => ({ preventDefault() {} });

function order(id = 'book', changes = {}) {
  return { id, filename: id + '.epub', order_no: id, status: 'cancelled', price_cny: '19.99', price_scope: '订单价格',
    created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T01:00:00Z',
    payment: { status: 'paid', amount: '19.99', checked_at: '2026-10-02T01:00:00Z' },
    cost: { note: '历史用量不完整', ledger: { requests: 1, priced_requests: 0, stages: {}, coverage: 'partial' } },
    files: { source: true, output: false }, checkout: {}, stages: [],
    payment_resolution: { state: 'paid_review', amount: '19.99', verified_at: '2026-10-02T01:00:00Z', original_cancel_message: '用户取消', original_error_code: 'CANCELLED' },
    review: { order_no: id, revision: 2, context: 'opaque-context', state: 'open', needs_attention: true,
      reasons: [{ code: 'paid_review', label: '已付取消单待人工处理' }, { code: 'unknown_channel', label: '旧支付通道未知' }, { code: 'ledger_gap', label: '费用记录存在缺口' }],
      allowed_actions: ['note', 'fulfill', 'record_external_refund', 'close_review'], scope_count: 1 }, ...changes };
}

function setup(initial) {
  const roots = new Map(), calls = [], created = [], confirmations = [], orders = new Map();
  class Element {
    constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this._text = ''; this.id = ''; this.value = ''; this.hidden = false; this.disabled = false; this.checked = false; this.open = false; this.listeners = {}; this.className = ''; created.push(this); }
    set textContent(value) { this._text = String(value ?? ''); this.children = []; }
    get textContent() { return this._text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
    set innerHTML(_) { throw new Error('Admin rendering must never use innerHTML'); }
    get innerHTML() { return ''; }
    get childNodes() { return this.children; }
    append(...children) { for (const child of children) { this.children.push(child); if (typeof child !== 'string') child.parentNode = this; } }
    replaceChildren(...children) { this.children.forEach(child => { if (typeof child !== 'string') child.parentNode = null; }); this.children = []; this._text = ''; this.append(...children); }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    close() { this.open = false; }
    showModal() { this.open = true; }
  }
  const walk = (element, fn) => { if (fn(element)) return element; for (const child of element.children) if (typeof child !== 'string') { const result = walk(child, fn); if (result) return result; } return null; };
  for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) { const element = new Element(id === 'detail' ? 'dialog' : 'div'); element.id = id; roots.set(id, element); }
  const $ = id => { for (const root of roots.values()) { const found = walk(root, child => child.id === id); if (found) return found; } return null; };
  let sequence = 0;
  const context = {
    document: { getElementById: $, createElement: tag => new Element(tag) }, URLSearchParams, Uint8Array,
    crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}` },
    formatLedgerCost: () => '待核实', confirm: message => { confirmations.push(message); return context.confirmResult; },
    confirmResult: true, setTimeout: fn => fn(), FormData: function () { return Object.entries(context.filters); }, filters: {},
    fetch: async (url, options = {}) => { calls.push({ url, options }); return context.respond(url, options); },
    respond: (url, options) => {
      if (url === '/api/admin/session') return initial ? initial.promise : response({ detail: '请登录管理员账号' }, 401);
      if (url.startsWith('/api/admin/orders?')) return response({ items: [...orders.values()], total: orders.size });
      if (url.includes('/review-history?')) return response({ items: [], next_cursor: null });
      if (options.method === 'POST') return response(orders.get(decodeURIComponent(url.split('/')[4])) || order());
      return response(orders.get(decodeURIComponent(url.split('/')[4])) || order());
    },
  };
  vm.createContext(context); vm.runInContext(source, context);
  const ready = (async () => { await tick(); if (!initial) { context.loggedIn({ username: 'operator', csrf: 'session-secret' }); calls.length = 0; } })();
  return { context, $, calls, created, confirmations, orders, ready, walk,
    async open(value = order()) { orders.set(value.id, value); await context.detail(value.id); },
    select(action) { $('review-action').value = action; $('review-action').onchange(); },
    submit() { return $('review-submit').parentNode.onsubmit(event()); },
    body() { return $('detail-body').textContent; },
  };
}

test('实际 DOM 显示付款事实、异常原因、费用缺口与服务端允许的动作', async () => {
  const h = setup(); await h.ready; const data = order(); data.review.allowed_actions = ['note', 'unrecognized']; await h.open(data);
  for (const value of ['取消后待人工处理（尚未退款）', '¥19.99', '用户取消', '旧支付通道未知', '费用记录存在缺口', '不按零费用处理']) assert(h.body().includes(value), value);
  assert.deepEqual(h.$('review-action').children.map(option => option.value), ['note']);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
  assert(h.body().includes('已记录部分'));
});

test('未知处置协议和无允许动作均不自行开放履约按钮', async () => {
  for (const review of [null, { ...order().review, allowed_actions: [] },
    { ...order().review, revision: '2' }, { ...order().review, context: '' }, { ...order().review, order_no: null }]) {
    const h = setup(); await h.ready; await h.open(order('book', { review }));
    assert.equal(h.$('review-submit'), null); assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
  }
});

test('坏历史元数据详情保留原状态或未知，仅显示诊断且全部操作禁用', async () => {
  for (const [status, expected] of [['historical-state', 'historical-state'], ['failed', '失败'], ['', '未知']]) {
    const h = setup(); await h.ready;
    // Even stale optimistic action/file flags cannot override the diagnostic marker.
    await h.open(order('broken', { metadata_invalid: true, status, files: { source: true, output: true },
      message: '历史元数据异常', cost: { ledger: null, estimated_usd: null, note: '费用未知，不按零费用处理' } }));
    const label = h.walk(h.$('detail-body'), el => el.tagName === 'DT' && el.textContent === '任务状态');
    assert.equal(label.parentNode.children[1].textContent, expected);
    assert(h.body().includes('¥19.99')); assert(h.body().includes('只读诊断')); assert(h.body().includes('费用未知'));
    assert.equal(h.$('review-submit'), null);
    assert.equal(h.walk(h.$('detail-body'), el => el.tagName === 'A' && el.className === 'file-link'), null);
    const count = h.calls.length;
    for (const text of ['核验支付宝支付', '重试失败订单', '查看逐请求费用明细', '查看人工处理历史']) {
      const button = h.walk(h.$('detail-body'), el => el.tagName === 'BUTTON' && el.textContent === text);
      assert(button, text); assert.equal(button.disabled, true, text);
      await button.onclick(); assert.equal(button.disabled, true, text);
    }
    assert.equal(h.calls.length, count); assert.equal(h.confirmations.length, 0);
  }
});

test('未登记的 open 不冒称已登记；关闭后新异常与外部退款边界显示准确', async () => {
  const h = setup(); await h.ready; const d = order();
  d.review.needs_attention = false; d.review.reasons = []; await h.open(d);
  assert(h.body().includes('人工跟进开放')); assert(!h.body().includes('已登记待处理工单'));
  d.review = { ...d.review, state: 'closed', needs_attention: true, allowed_actions: ['note'] };
  d.payment_resolution = { state: 'external_refund_recorded' }; await h.open(d);
  assert(h.body().includes('此前工单已登记关闭')); assert(h.body().includes('当前事实或异常需要重新核对'));
  assert(h.body().includes('已登记外部全额退款（未经网关退款核验）'));
  assert.deepEqual(h.$('review-action').children.map(option => option.value), ['note']);
});

test('异常筛选通过原列表接口发送，整批状态不重复计算价格', async () => {
  const h = setup(); await h.ready; const d = order(); d.review.scope_count = 3; d.review.order_no = 'batch_books'; d.batch_id = 'books'; h.orders.set(d.id, d);
  h.context.filters = { review: 'paid_review', q: '书' }; await h.context.load();
  assert(h.calls[0].url.includes('review=paid_review')); assert(h.$('rows').textContent.includes('需要人工处理'));
  await h.open(d); assert(h.body().includes('3 个任务')); assert(h.body().includes('不得按子任务重复登记退款'));
  assert(html.includes('value="resolved"')); assert(html.includes('value="open"')); assert(!html.includes('value="needs_attention"'));
});

test('履约须确认费用且只提交原上下文，管理员身份不可由浏览器指定', async () => {
  const h = setup(); await h.ready; await h.open(); h.select('fulfill'); h.$('review-note').value = '已获客户确认';
  await h.submit(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.$('review-acknowledge').checked = true; await h.submit(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.$('review-evidence').value = '客户确认恢复履约的记录'; await h.submit();
  const call = h.calls.find(c => c.url.endsWith('/review')); const body = JSON.parse(call.options.body);
  assert.equal(call.url, '/api/admin/orders/book/review'); assert.equal(call.options.headers['X-CSRF-Token'], 'session-secret');
  assert.equal(call.options.credentials, 'same-origin'); assert.equal(call.options.cache, 'no-store');
  assert.equal(body.action, 'fulfill'); assert.equal(body.expected_revision, 2); assert.equal(body.expected_context, 'opaque-context');
  assert.equal(body.acknowledge_cost, true); assert.equal(body.actor, undefined); assert.equal(body.amount, undefined);
  assert(/^[0-9a-f-]{36}$/.test(body.request_id)); assert(h.confirmations[0].includes('冻结金额')); assert(h.confirmations[0].includes('模型费用'));
});

test('拒绝确认不发送；登记外部退款必须填写凭据与参考号且明确非网关核验', async () => {
  const h = setup(); await h.ready; await h.open(); h.select('record_external_refund'); h.$('review-note').value = '人工处理';
  assert(h.body().includes('不调用退款接口，未经网关退款核验')); assert(h.body().includes('不得把关单'));
  await h.submit(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.$('review-evidence').value = '线下凭证编号'; await h.submit(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.$('review-refund-reference').value = 'REF-001'; h.context.confirmResult = false; await h.submit(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.context.confirmResult = true; await h.submit(); const body = JSON.parse(h.calls.find(c => c.url.endsWith('/review')).options.body);
  assert.equal(body.refund_reference, 'REF-001'); assert.equal(body.evidence, '线下凭证编号'); assert.equal(body.acknowledge_cost, false);
  assert(!h.calls.some(c => /refund|precreate|continue-payment/.test(c.url)));
});

test('结束人工跟进要求说明与依据，关单不会被解释为退款', async () => {
  const h = setup(); await h.ready; await h.open(order('book', { payment_resolution: { state: 'closed' } })); h.select('close_review');
  assert(h.body().includes('网关已关单，不代表已退款')); assert(h.body().includes('不代表已退款、已交付'));
  h.$('review-note').value = '无需继续联系'; await h.submit(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.$('review-evidence').value = '客户已确认说明'; await h.submit(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 1);
});

test('双击及离开再回原订单不重复提交进行中的操作', async () => {
  const h = setup(); await h.ready; await h.open(); const gate = deferred(), original = h.context.respond;
  h.context.respond = (url, options) => url.endsWith('/review') ? gate.promise : original(url, options);
  h.$('review-note').value = '跟进'; const first = h.submit(); assert.equal(h.$('review-submit').disabled, true); await h.submit();
  await h.open(order('other')); await h.open(); h.$('review-note').value = '另一次跟进'; await h.submit();
  assert.equal(h.calls.filter(c => c.url.endsWith('/review')).length, 1); assert(h.$('review-status').textContent.includes('已有操作提交中'));
  gate.resolve(response(order())); await first; assert(h.body().includes('book.epub'));
});

test('断网或 503 后相同载荷复用 UUID，说明改变则生成新请求', async () => {
  for (const kind of ['network', '503']) {
    const h = setup(); await h.ready; await h.open(); const original = h.context.respond;
    h.context.respond = (url, options) => { if (url.endsWith('/review')) { if (kind === 'network') throw new Error('连接中断'); return response({ detail: '暂不可用' }, 503); } return original(url, options); };
    h.$('review-note').value = '跟进'; await h.submit(); await h.submit(); h.$('review-note').value = '补充说明'; await h.submit();
    const bodies = h.calls.filter(c => c.url.endsWith('/review')).map(c => JSON.parse(c.options.body));
    assert.equal(bodies[0].request_id, bodies[1].request_id); assert.notEqual(bodies[1].request_id, bodies[2].request_id);
    assert(h.$('review-status').textContent.includes('复用本次请求编号')); assert.equal(h.$('review-submit').disabled, false);
  }
});

test('模糊失败后关闭再打开同一详情，同动作载荷仍复用请求编号', async () => {
  const h = setup(); await h.ready; await h.open(); const original = h.context.respond;
  h.context.respond = (url, options) => url.endsWith('/review') ? response({ detail: '确认响应失败' }, 503) : original(url, options);
  h.$('review-note').value = '跟进'; await h.submit(); h.$('close').onclick(); await h.open();
  h.$('review-note').value = '跟进'; await h.submit();
  const bodies = h.calls.filter(c => c.url.endsWith('/review')).map(c => JSON.parse(c.options.body));
  assert.equal(bodies[0].request_id, bodies[1].request_id);
});

test('409 刷新真实详情与列表，不自动重放变更后的动作', async () => {
  const h = setup(); await h.ready; await h.open(); const original = h.context.respond;
  h.context.respond = (url, options) => { if (url.endsWith('/review')) { const d = order(); d.review = { ...d.review, revision: 3, allowed_actions: [], needs_attention: false, state: 'closed' }; h.orders.set('book', d); return response({ detail: '状态变化' }, 409); } return original(url, options); };
  h.$('review-note').value = '旧说明'; await h.submit(); assert.equal(h.calls.filter(c => c.url.endsWith('/review')).length, 1);
  assert.equal(h.$('review-submit'), null); assert(h.$('error').textContent.includes('已刷新详情')); assert(h.body().includes('已登记关闭工单'));
});

test('人工历史显式分页、防重点击、游标编码、重叠去重且全部文字渲染', async () => {
  const h = setup(); await h.ready; await h.open(); const original = h.context.respond, gate = deferred(); let page = 0;
  const row = { id: 'a', action: 'note', actor: '<img src=x onerror=evil()>', note: '<script>evil()</script>', evidence: '证据', refund_reference: 'R-1', created_at: '2026-10-02T00:00:00Z', result: { state: 'open' } };
  h.context.respond = (url, options) => url.includes('/review-history?') ? (++page === 1 ? gate.promise : response({ items: [row, { ...row, id: 'b', actor: 'second' }], next_cursor: null })) : original(url, options);
  const more = h.$('review-history-more'), first = more.onclick(); await more.onclick(); assert.equal(page, 1);
  gate.resolve(response({ items: [row], next_cursor: 'opaque /?&cursor' })); await first; assert.equal(more.hidden, false); await more.onclick();
  const calls = h.calls.filter(c => c.url.includes('/review-history?')); assert(calls[1].url.includes('before=opaque+%2F%3F%26cursor')); assert(calls[0].url.includes('limit=20'));
  assert.equal(h.created.filter(c => c.tagName === 'ARTICLE').length, 2); assert(h.body().includes(row.actor)); assert(h.body().includes(row.note));
  assert(!h.created.some(c => c.tagName === 'SCRIPT' || c.tagName === 'IMG')); assert.equal(more.hidden, true);
});

test('历史分页失败保留已加载记录，再试使用原游标', async () => {
  const h = setup(); await h.ready; await h.open(); const original = h.context.respond; let page = 0;
  h.context.respond = (url, options) => url.includes('/review-history?') ? (++page === 1 ? response({ items: [{ id: 'a', action: 'note', note: '已加载', created_at: '2026-10-02T00:00:00Z' }], next_cursor: 'cursor-one' }) : response({ detail: '暂不可读' }, 503)) : original(url, options);
  const more = h.$('review-history-more'); await more.onclick(); await more.onclick(); await more.onclick();
  const calls = h.calls.filter(c => c.url.includes('/review-history?')); assert.equal(calls[1].url, calls[2].url); assert(h.body().includes('已加载')); assert(h.body().includes('暂不可读')); assert.equal(more.disabled, false);
});

test('较早详情响应或失败不能覆盖较新订单与错误提示', async () => {
  for (const code of [200, 503]) {
    const h = setup(); await h.ready; const gate = deferred(), original = h.context.respond;
    h.context.respond = (url, options) => url === '/api/admin/orders/old' ? gate.promise : original(url, options);
    const old = h.context.detail('old'); await h.open(order('new')); h.$('error').textContent = '新提示';
    gate.resolve(response(code === 200 ? order('old') : { detail: '旧错误' }, code)); await old;
    assert(h.body().includes('new.epub')); assert(!h.body().includes('old.epub')); assert.equal(h.$('error').textContent, '新提示');
  }
});

test('关闭详情后迟到响应不能重新弹窗', async () => {
  const h = setup(); await h.ready; const gate = deferred(); h.context.respond = () => gate.promise;
  const work = h.context.detail('book'); h.$('close').onclick(); gate.resolve(response(order())); await work;
  assert.equal(h.$('detail').open, false); assert(!h.body().includes('book.epub'));
});

test('旧订单的历史和原支付核验响应不会抢回新订单详情', async () => {
  for (const kind of ['history', 'payment']) {
    const h = setup(); await h.ready; await h.open(); const gate = deferred(), original = h.context.respond;
    h.context.respond = (url, options) => url.includes('/review-history?') || url.endsWith('/payment') ? gate.promise : original(url, options);
    const control = kind === 'history' ? h.$('review-history-more') : h.walk(h.$('detail-body'), el => el.tagName === 'BUTTON' && el.textContent === '核验支付宝支付');
    const work = control.onclick(); await h.open(order('other')); const count = h.calls.length;
    gate.resolve(response(kind === 'history' ? { items: [{ id: 'old', note: '旧历史秘密' }], next_cursor: null } : order())); await work;
    assert(h.body().includes('other.epub')); assert(!h.body().includes('旧历史秘密')); assert.equal(h.calls.length, count);
  }
});

test('409 刷新途中切换登录，旧处理结果不污染新会话', async () => {
  const h = setup(); await h.ready; await h.open(); const gate = deferred(), original = h.context.respond;
  h.context.respond = (url, options) => url.endsWith('/review') ? response({ detail: '状态变化' }, 409) : url === '/api/admin/orders/book' ? gate.promise : original(url, options);
  h.$('review-note').value = '跟进'; const work = h.submit(); await tick();
  h.context.showLogin(); h.context.loggedIn({ username: 'new', csrf: 'different' }); await h.open(order('other')); h.$('error').textContent = '新会话提示';
  const count = h.calls.length; gate.resolve(response(order())); await work;
  assert.equal(h.$('error').textContent, '新会话提示'); assert(h.body().includes('other.epub')); assert.equal(h.calls.length, count);
});

test('退出后迟到列表、详情、历史和提交响应不能恢复私有界面', async () => {
  for (const kind of ['list', 'detail', 'history', 'review']) {
    const h = setup(); await h.ready; await h.open(); const gate = deferred(), original = h.context.respond;
    h.context.respond = (url, options) => url === '/api/admin/logout' ? response({ ok: true }) : gate.promise;
    let work;
    if (kind === 'list') work = h.context.load();
    else if (kind === 'detail') work = h.context.detail('other');
    else if (kind === 'history') work = h.$('review-history-more').onclick();
    else { h.$('review-note').value = '私有说明'; work = h.submit(); }
    await h.$('logout').onclick(); gate.resolve(response(kind === 'list' ? { items: [order()], total: 1 } : kind === 'history' ? { items: [{ id: 'one', note: '私有' }], next_cursor: null } : order())); await work;
    assert.equal(h.$('dashboard').hidden, true); assert.equal(h.$('rows').textContent, ''); assert.equal(h.body(), ''); assert.equal(h.$('detail').open, false);
    assert.equal(h.calls.filter(c => c.url.endsWith('/review')).length, kind === 'review' ? 1 : 0);
  }
});

test('旧会话的迟到 401 不退出新登录，旧 session 初始化不覆盖显式登录', async () => {
  const h = setup(); await h.ready; const gate = deferred(), original = h.context.respond;
  h.context.respond = (url, options) => url === '/api/admin/orders/old' ? gate.promise : original(url, options);
  const work = h.context.detail('old'); h.context.loggedIn({ username: 'new-operator', csrf: 'new-session' }); await h.open(order('new'));
  gate.resolve(response({ detail: '旧会话失效' }, 401)); await work; assert.equal(h.$('username').textContent, 'new-operator'); assert.equal(h.$('dashboard').hidden, false);
  const initial = deferred(), other = setup(initial); await other.ready; other.context.loggedIn({ username: 'explicit', csrf: 'explicit-session' });
  initial.resolve(response({ username: 'stale', csrf: 'old-session' })); await tick(); assert.equal(other.$('username').textContent, 'explicit');
});

test('登录失败文字正常显示而不吞掉 401 原因', async () => {
  const h = setup(); await h.ready; h.context.showLogin(); const original = h.context.respond;
  h.context.respond = (url, options) => url === '/api/admin/login' ? response({ detail: '用户名或密码错误' }, 401) : original(url, options);
  await h.$('login-form').onsubmit(event()); assert.equal(h.$('error').textContent, '用户名或密码错误'); assert.equal(h.$('login-button').disabled, false);
});
