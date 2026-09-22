const checkout = require('../repair-checkout');
const later = require('../return-later');
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../epub-repair.html'), 'utf8');
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const report = { total_issues: 1, fixable_count: 1, unfixable_count: 0, epub_version: '3', issues: [] };
const pending = (price = '2.99') => ({ status: 'pending_payment', can_pay: true, price_cny: price, report });
class Element {
  constructor(id) { this.id = id; this.children = []; this.listeners = {}; this.attributes = {}; this.style = {}; this.hidden = false; this.disabled = false; this.value = ''; this._text = ''; this.classes = new Set(); this.classList = { add: value => this.classes.add(value), remove: value => this.classes.delete(value), contains: value => this.classes.has(value) }; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { this.children = []; this._text = value; }
  get innerHTML() { return this._text; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; this._text = ''; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] || null; }
  removeAttribute(key) { delete this.attributes[key]; }
  set href(value) { this.setAttribute('href', value); } get href() { return this.getAttribute('href') || ''; }
  set src(value) { this.setAttribute('src', value); } get src() { return this.getAttribute('src') || ''; }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  async dispatch(event, data = {}) { if (event === 'click' && this.disabled) return; await Promise.all((this.listeners[event] || []).map(cb => cb({ preventDefault() {}, ...data }))); }
  click() { return this.dispatch('click'); }
}
function harness(config = {}) {
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element(match[1])]));
  for (const id of ['qrPayment', 'webPayment', 'checkPaymentBtn']) elements[id].hidden = true;
  elements.priceLabel.textContent = '¥2.99';
  elements.payBtn.appendChild(elements.payButtonLabel); elements.payBtn.appendChild(elements.priceLabel);
  elements.payBox.appendChild(elements.payPrice); elements.payBox.appendChild(elements.qrPayment); elements.payBox.appendChild(elements.webPayment);
  elements.qrPayment.appendChild(elements.qrImg); elements.webPayment.appendChild(elements.alipayLink);
  const states = new Map(); const calls = []; const history = []; const reports = []; const windows = [];
  const listeners = {}; const documentListeners = {};
  let time = 100000, nextTimer = 1, page = '';
  const timers = new Map();
  const storage = config.storage || { getItem() { return null; } };
  const win = { FormData: class { constructor() { this.values = {}; } append(k, v) { this.values[k] = v; } },
    screenX: 0, screenY: 0, outerWidth: 1000, outerHeight: 800,
    addEventListener(event, callback) { (listeners[event] ||= []).push(callback); },
    open(url) { const popup = { url, closed: false, focus() {}, close() { this.closed = true; } }; windows.push(popup); return popup; },
  };
  const doc = { getElementById: id => elements[id], visibilityState: 'visible', addEventListener(event, callback) { documentListeners[event] = callback; } };
  const controller = checkout.mount({ window: win, document: doc, storage, sessionStorage: storage,
    now: () => time, setTimeout: (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, at: time + ms }); return id; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, opts = {}) => {
      const call = { url, ...opts }; calls.push(call);
      if (config.fetch) { const value = config.fetch(call); if (value !== undefined) return value; }
      if (url.endsWith('/checkout-events')) return response({ recorded: true });
      const id = url.split('/')[4];
      if (url.endsWith('/status')) return response(states.get(id) || pending());
      if (url.endsWith('/recover')) return response({ ...(states.get(id) || pending()), payment_check: 'pending', retry_after_seconds: 45 });
      if (url.endsWith('/pay')) return response({ status: 'pending_payment', price_cny: '2.99', pay_url: 'https://openapi.alipay.com/gateway.do?order=' + id });
      if (url.endsWith('/diagnose')) return response({ ...pending(), job_id: opts.body.values.file.id || 'new' });
      throw Error('Unexpected request ' + url);
    },
    authHeaders: id => ({ 'X-Job-Token': 'token:' + id }),
    QRCode: { toDataURL: config.qr || (async code => 'data:image/png;base64,' + code) },
    returnLater: { setTask(task) { history.push(task); }, remember(task) { history.push(task); } },
    onTask: id => { page = id; }, onClear: () => { page = ''; },
    renderReport(value) { reports.push(value); elements.reportBlock.classList.add('visible'); elements.issueArea.style.display = 'block'; },
  });
  return { controller, elements, states, calls, reports, history, timers, windows, get page() { return page; },
    el: id => elements[id],
    async focus() { await Promise.all((listeners.focus || []).map(fn => fn())); },
    async visible() { await documentListeners.visibilitychange(); await flush(); },
    async advance(ms) {
      const target = time + ms;
      let steps = 0;
      while (true) {
        const next = [...timers.entries()].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        if (++steps > 1000) throw Error('Timer loop');
        timers.delete(next[0]); time = next[1].at; await next[1].fn(); await flush();
      }
      time = target;
    },
  };
}
const upload = (h, id = 'new') => h.controller.handleFile({ name: id + '.epub', size: 1000, id });
const eventNames = h => h.calls.filter(call => call.url.endsWith('/checkout-events')).map(call => JSON.parse(call.body).event);

test('F25-1 发起支付失败后按钮恢复且原 priceLabel 节点始终保留', async () => {
  let fail = true;
  const h = harness({ fetch: call => call.url.endsWith('/pay') ? response(fail ? { detail: '临时失败' } : { pay_url: 'https://openapi.alipay.com/pay', price_cny: '5.99' }, fail ? 503 : 200) : undefined });
  h.states.set('old', pending('5.99')); await h.controller.resume('old');
  const label = h.el('priceLabel');
  await h.el('payBtn').click();
  assert.equal(h.el('payBtn').disabled, false); assert.equal(h.el('payButtonLabel').textContent, '🔧 一键修复');
  assert.ok(h.el('payBtn').children.includes(label)); assert.equal(label.textContent, '¥5.99');
  fail = false; await h.el('payBtn').click();
  assert.equal(h.el('payButtonLabel').textContent, '重新获取支付入口'); assert.equal(h.el('payBtn').disabled, false);
  assert.equal(h.el('webPayment').hidden, false); assert.equal(label.textContent, '¥5.99');
});

test('F25-2 网页支付后重新上传另一书，二维码和新价格使用仍连接的原 DOM', async () => {
  const h = harness({ fetch: call => call.url.endsWith('/second/pay') ? response({ qr_code: 'second-qr', price_cny: '1.99' }) : undefined });
  await h.controller.resume('first'); await h.el('payBtn').click();
  const qr = h.el('qrImg'), amount = h.el('payPrice');
  assert.equal(h.el('webPayment').hidden, false);
  await h.el('resetBtn').click(); await upload(h, 'second'); await h.el('payBtn').click();
  assert.equal(h.el('qrPayment').hidden, false); assert.equal(h.el('webPayment').hidden, true);
  assert.equal(h.el('alipayLink').href, ''); assert.equal(qr.src, 'data:image/png;base64,second-qr');
  assert.ok(h.el('qrPayment').children.includes(qr)); assert.ok(h.el('payBox').children.includes(amount));
  assert.equal(amount.textContent, '¥1.99 / 次');
});

test('F25-3 旧 pay 响应晚于新上传，不得显示旧链接或更改新报价', async () => {
  const late = deferred();
  const h = harness({ fetch: call => call.url.endsWith('/first/pay') ? late.promise : undefined });
  await h.controller.resume('first'); const paying = h.el('payBtn').click(); await flush();
  assert.equal(h.el('payBtn').disabled, true);
  await upload(h, 'second');
  late.resolve(response({ pay_url: 'https://openapi.alipay.com/old', price_cny: '5.99' })); await paying;
  assert.equal(h.page, 'second'); assert.equal(h.el('priceLabel').textContent, '¥2.99');
  assert.equal(h.el('alipayLink').href, ''); assert.equal(h.el('payBox').classList.contains('visible'), false);
  assert.equal(h.el('payBtn').disabled, false);
});

test('F25-4 旧 pay 错误晚于切换历史任务，不覆盖新任务文案和按钮', async () => {
  const late = deferred(); const h = harness({ fetch: call => call.url.endsWith('/first/pay') ? late.promise : undefined });
  await h.controller.resume('first'); const paying = h.el('payBtn').click(); await flush();
  h.states.set('done', { status: 'repaired', can_pay: false }); await h.controller.resume('done');
  late.resolve(response({ detail: '旧任务失败' }, 500)); await paying;
  assert.equal(h.el('diagStatus').textContent, ''); assert.equal(h.el('payBtn').disabled, true);
  assert.equal(h.el('downloadBtn').href, '/api/v2/repair/done/download');
});

test('F25-5 二维码异步生成中切换任务，旧图不能污染新任务', async () => {
  const late = deferred(); const h = harness({ qr: () => late.promise, fetch: call => call.url.endsWith('/pay') ? response({ qr_code: 'old', price_cny: '5.99' }) : undefined });
  await h.controller.resume('first'); const paying = h.el('payBtn').click(); await flush();
  await h.controller.resume('second'); late.resolve('data:image/png;base64,old'); await paying;
  assert.equal(h.el('qrImg').src, ''); assert.equal(h.el('payBox').classList.contains('visible'), false);
  assert.equal(h.el('priceLabel').textContent, '¥2.99');
});

test('F25-6 旧 status 失败/成功响应均不能改变当前任务', async () => {
  for (const result of [response({ detail: 'gone' }, 404), response({ status: 'repaired', price_cny: '5.99' })]) {
    const late = deferred(); const h = harness({ fetch: call => call.url.endsWith('/first/status') ? late.promise : undefined });
    const restoring = h.controller.resume('first'); await flush(); await h.controller.resume('second');
    late.resolve(result); await restoring;
    assert.equal(h.page, 'second'); assert.equal(h.el('priceLabel').textContent, '¥2.99');
    assert.ok(!h.el('fixStatus').textContent.includes('不存在')); assert.equal(h.el('downloadArea').style.display, 'none');
  }
});

test('F25-7 两次诊断响应逆序返回，仅最后选择文件可成为当前订单', async () => {
  const late = deferred(); const h = harness({ fetch: call => call.url.endsWith('/diagnose') && call.body.values.file.id === 'first' ? late.promise : undefined });
  const first = upload(h, 'first'); await upload(h, 'second');
  late.resolve(response({ ...pending('5.99'), job_id: 'first' })); await first;
  assert.equal(h.page, 'second'); assert.equal(h.el('priceLabel').textContent, '¥2.99');
  assert.ok(!h.history.some(task => task && task.id === 'first'));
});

test('F25-8 刷新无法修复或无需修复文件，can_pay=false 始终禁用付款', async () => {
  for (const details of [{ total_issues: 1, fixable_count: 0, unfixable_count: 1, issues: [] }, { total_issues: 0, fixable_count: 0, unfixable_count: 0, issues: [] }]) {
    const h = harness(); h.states.set('bad', { ...pending(), can_pay: false, report: details });
    await h.controller.resume('bad'); await h.advance(6000); await h.el('payBtn').click();
    assert.equal(h.el('payBtn').disabled, true); assert.equal(h.el('checkPaymentBtn').hidden, true);
    assert.equal(h.calls.filter(call => call.url.endsWith('/pay') || call.url.endsWith('/recover')).length, 0);
    assert.deepEqual(eventNames(h), []);
  }
});

test('F25-9 recover unavailable 不说未付款，状态轮询不擦掉此提示', async () => {
  const h = harness({ fetch: call => call.url.endsWith('/recover') ? response({ ...pending(), payment_check: 'unavailable', retry_after_seconds: 60 }) : undefined });
  await h.controller.resume('book');
  assert.ok(h.el('fixStatus').textContent.includes('暂时无法可靠确认')); assert.ok(h.el('fixStatus').textContent.includes('请勿重复支付'));
  await h.advance(6000);
  assert.ok(h.el('fixStatus').textContent.includes('暂时无法可靠确认')); assert.ok(!h.el('fixStatus').textContent.includes('未付款'));
});

test('F25-10 恢复查询 HTTP/network 错误保留待确认含义，不启用重复处理', async () => {
  for (const failure of [() => response({}, 503), () => Promise.reject(Error('offline'))]) {
    const h = harness({ fetch: call => call.url.endsWith('/recover') ? failure() : undefined }); await h.controller.resume('book');
    assert.ok(h.el('fixStatus').textContent.includes('请勿重复支付'));
    assert.equal(h.el('downloadArea').style.display, 'none');
  }
});

test('F25-11 自动补查有间隔且最多十次，聚焦/手动/3秒轮询不能绕开', async () => {
  const h = harness(); await h.controller.resume('book');
  for (let i = 0; i < 5; i++) { await h.focus(); await h.el('checkPaymentBtn').click(); }
  await h.advance(44000);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 1);
  await h.advance(2000);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 2);
  await h.advance(600000); await h.focus(); await h.el('checkPaymentBtn').click();
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 10);
  assert.ok(h.calls.filter(call => call.url.endsWith('/status')).length > 50);
  assert.ok(h.el('fixStatus').textContent.includes('自动查询已暂停'));
});

test('F25-12 未开始付款时停止自动网关补查，用户真实点击后恢复', async () => {
  const h = harness({ fetch: call => call.url.endsWith('/recover') ? response({ ...pending(), payment_check: 'not_started' }) : undefined });
  await h.controller.resume('book'); await h.advance(120000);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 1);
  await h.el('payBtn').click(); await h.advance(3000);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 2);
});

test('F25-13 支付弹窗返回或页面重新可见会读服务端结果，确认后关闭弹窗并下载', async () => {
  const h = harness(); await h.controller.resume('book'); await h.el('payBtn').click(); await h.el('alipayLink').click();
  assert.equal(h.windows.length, 1);
  h.states.set('book', { status: 'paid', can_pay: false }); await h.focus();
  assert.equal(h.windows[0].closed, true); assert.equal(h.el('payBtn').disabled, true);
  assert.ok(h.el('fixStatus').textContent.includes('支付已确认'));
  h.states.set('book', { status: 'repaired', can_pay: false }); await h.visible();
  assert.equal(h.el('downloadArea').style.display, 'block'); assert.equal(h.el('downloadBtn').href, '/api/v2/repair/book/download');
});

test('F25-14 无法访问浏览器 storage 时仍可上传支付，不能擅改为测试价', async () => {
  const blockedHost = { get localStorage() { throw Error('SecurityError'); } };
  for (const storage of [later.safeStorage(blockedHost), { getItem() { throw Error('Storage unavailable'); } }]) {
    const h = harness({ storage }); await upload(h); await h.el('payBtn').click();
    assert.equal(h.el('webPayment').hidden, false); assert.equal(h.el('priceLabel').textContent, '¥2.99');
    assert.equal(h.calls.find(call => call.url.endsWith('/pay')).body, undefined);
  }
});

test('F25-15 漏斗只记录可付款报价/真实点击，轮询恢复不能伪造点击或成功', async () => {
  const h = harness(); await h.controller.resume('book'); await h.advance(9000); await h.focus();
  assert.deepEqual(eventNames(h), ['quote_shown']);
  await h.el('payBtn').click(); await h.el('alipayLink').click();
  assert.deepEqual(eventNames(h), ['quote_shown', 'payment_clicked']);
  const events = h.calls.filter(call => call.url.endsWith('/checkout-events'));
  assert.ok(events.every(call => call.headers['X-Job-Token'] === 'token:book'));
  assert.ok(!eventNames(h).includes('payment_succeeded'));
});

test('F25-16 二维码组件异常/缺支付入口恢复按钮，价格标签不消失', async () => {
  for (const payload of [{ qr_code: 'qr' }, {}]) {
    const h = harness({ qr: async () => { throw Error('QR failed'); }, fetch: call => call.url.endsWith('/pay') ? response(payload) : undefined });
    await h.controller.resume('book'); await h.el('payBtn').click();
    assert.equal(h.el('payBtn').disabled, false); assert.equal(h.el('payButtonLabel').textContent, '🔧 一键修复');
    assert.ok(h.el('payBtn').children.includes(h.el('priceLabel'))); assert.ok(h.el('diagStatus').textContent.includes('支付发起失败'));
  }
});

test('F25-17 过期/无权限订单停止读取并禁止支付，不影响别的订单', async () => {
  for (const status of [403, 404]) {
    const h = harness({ fetch: call => call.url.endsWith('/old/status') ? response({}, status) : undefined });
    await h.controller.resume('old'); await h.advance(9000);
    assert.equal(h.el('payBtn').disabled, true); assert.equal(h.calls.filter(call => call.url.endsWith('/old/status')).length, 1);
    await h.controller.resume('new'); assert.equal(h.el('payBtn').disabled, false);
  }
});

test('F25-18 网关返回其他站点或非 HTTPS 链接不得展示', async () => {
  for (const pay_url of ['javascript:alert(1)', 'https://evilalipay.com/pay', 'http://openapi.alipay.com/pay']) {
    const h = harness({ fetch: call => call.url.endsWith('/pay') ? response({ pay_url }) : undefined });
    await h.controller.resume('book'); await h.el('payBtn').click();
    assert.equal(h.el('alipayLink').href, ''); assert.equal(h.el('webPayment').hidden, true); assert.equal(h.el('payBtn').disabled, false);
  }
});

test('F25-19 旧 recover 响应晚于任务切换不展示旧完成文件', async () => {
  const late = deferred(); const h = harness({ fetch: call => call.url.endsWith('/old/recover') ? late.promise : undefined });
  const old = h.controller.resume('old'); await flush(); await h.controller.resume('new');
  late.resolve(response({ status: 'repaired', payment_check: 'verified_paid' })); await old;
  assert.equal(h.page, 'new'); assert.equal(h.el('downloadArea').style.display, 'none'); assert.equal(h.el('payBtn').disabled, false);
});

test('F25-20 手动确认服务器回报 verified_paid，按钮锁定并进入处理', async () => {
  let paid = false;
  const h = harness({ fetch: call => call.url.endsWith('/recover') ? response(paid ? { status: 'paid', can_pay: false, payment_check: 'verified_paid' } : { ...pending(), payment_check: 'pending' }) : undefined });
  await h.controller.resume('book'); await h.advance(45000); paid = true;
  h.states.set('book', { status: 'paid', can_pay: false }); await h.advance(45000); await h.el('checkPaymentBtn').click();
  assert.equal(h.el('payBtn').disabled, true); assert.ok(h.el('fixStatus').textContent.includes('支付已确认'));
});

test('F25-21 同任务旧 pending 状态晚于已验证付款返回，不得重新启用支付', async () => {
  const late = deferred(); let delayRead = false, paid = false;
  const h = harness({ fetch: call => {
    if (call.url.endsWith('/status') && delayRead) return late.promise;
    if (call.url.endsWith('/recover') && paid) return response({ status: 'paid', payment_check: 'verified_paid' });
  } });
  await h.controller.resume('book'); delayRead = true;
  const staleRead = h.focus(); await flush(); paid = true; await h.advance(45000);
  assert.equal(h.el('payBtn').disabled, true);
  late.resolve(response(pending())); await staleRead;
  assert.equal(h.el('payBtn').disabled, true); assert.ok(h.el('fixStatus').textContent.includes('支付已确认'));
});

test('F25-22 同任务 QR 绘制中被状态确认已付款，旧二维码不能重新展示', async () => {
  const late = deferred();
  const h = harness({ qr: () => late.promise, fetch: call => call.url.endsWith('/pay') ? response({ qr_code: 'qr' }) : undefined });
  await h.controller.resume('book'); const paying = h.el('payBtn').click(); await flush();
  h.states.set('book', { status: 'paid', can_pay: false }); await h.focus();
  late.resolve('data:image/png;base64,paid'); await paying;
  assert.equal(h.el('payBox').classList.contains('visible'), false); assert.equal(h.el('qrImg').src, '');
  assert.equal(h.el('payBtn').disabled, true); assert.ok(h.el('fixStatus').textContent.includes('支付已确认'));
});

test('F25-23 历史坏文件已有付款入口，可补查历史付款但不可再次发起支付', async () => {
  const h = harness();
  h.states.set('book', { ...pending(), can_pay: false, payment_started: true, report: { ...report, fixable_count: 0, unfixable_count: 1 } });
  await h.controller.resume('book');
  assert.equal(h.el('payBtn').disabled, true); assert.equal(h.el('checkPaymentBtn').hidden, false);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 1);
  assert.ok(h.el('fixStatus').textContent.includes('已有付款仍待核实'));
  await h.el('payBtn').click(); assert.equal(h.calls.filter(call => call.url.endsWith('/pay')).length, 0);
});

test('F25-24 刷新读取持久化查询失败，紧接限流不能将提示降为普通等待付款', async () => {
  const h = harness({ fetch: call => call.url.endsWith('/recover') ? response({ status: 'pending_payment', payment_check: 'throttled', retry_after_seconds: 60 }) : undefined });
  h.states.set('book', { ...pending(), payment_started: true, payment_check: 'unavailable' }); await h.controller.resume('book');
  assert.ok(h.el('fixStatus').textContent.includes('暂时无法可靠确认')); assert.ok(h.el('fixStatus').textContent.includes('请勿重复支付'));
});

test('F25-25 服务端明确未发起支付时只读状态，首次点击后才补查', async () => {
  const h = harness(); h.states.set('book', { ...pending(), payment_started: false });
  await h.controller.resume('book'); await h.advance(9000);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 0);
  await h.el('payBtn').click(); h.states.set('book', { ...pending(), payment_started: true }); await h.advance(3000);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 1);
});

test('F25-26 过了后台核实期限仍可人工联系，页面不再自动补查', async () => {
  const h = harness(); h.states.set('book', { ...pending(), payment_started: true, payment_check: 'manual_review_required' });
  await h.controller.resume('book'); await h.advance(60000);
  assert.equal(h.calls.filter(call => call.url.endsWith('/recover')).length, 0);
  assert.ok(h.el('fixStatus').textContent.includes('自动核实期限已结束'));
});

test('F25-27 支付时文件资格改变，409 后刷新报告并禁用付款', async () => {
  const h = harness({ fetch: call => {
    if (call.url.endsWith('/pay')) { h.states.set('book', { ...pending(), can_pay: false }); return response({ detail: '文件已损坏' }, 409); }
  } });
  await h.controller.resume('book'); await h.el('payBtn').click();
  assert.equal(h.el('payBtn').disabled, true); assert.ok(h.el('diagStatus').textContent.includes('文件已损坏'));
});

test('F25-28 同任务旧 status 错误晚于付款成功，不覆盖已确认状态', async () => {
  const late = deferred(); let delayRead = false;
  const h = harness({ fetch: call => {
    if (call.url.endsWith('/status') && delayRead) return late.promise;
    if (call.url.endsWith('/pay')) return response({ status: 'paid' });
  } });
  await h.controller.resume('book'); delayRead = true; const staleRead = h.focus(); await flush();
  await h.el('payBtn').click(); late.resolve(response({}, 404)); await staleRead;
  assert.ok(h.el('fixStatus').textContent.includes('支付已确认')); assert.equal(h.el('payBtn').disabled, true);
});
