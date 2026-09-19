const fs = require('fs');
const vm = require('vm');
const path = require('path');
const later = require('../return-later');
const library = require('../lib');
const index = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const repair = fs.readFileSync(path.join(__dirname, '../epub-repair.html'), 'utf8');
function nativeStorage() { const data = new Map(); return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) }; }
function tick() { return new Promise(resolve => setImmediate(resolve)); }
class Element {
  constructor() { this.children = []; this.listeners = {}; this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false; this.valid = true; }
  addEventListener(name, cb) { this.listeners[name] = cb; }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  checkValidity() { return this.valid; }
  reportValidity() { this.reported = true; }
}
function harness(fetcher, storage = later.safeStorage({ localStorage: nativeStorage() })) {
  const elements = new Map();
  const doc = { createElement() { return new Element(); } };
  const panel = new Element(); panel.ownerDocument = doc;
  panel.querySelector = selector => { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); };
  const calls = [];
  const control = later.mount({ root: panel, storage, loadHistory: library.loadHistory, saveHistory: library.saveHistory,
    authHeaders: id => ({ 'X-Job-Token': `token:${id}` }),
    fetch: async (url, options) => { calls.push({ url, ...options }); return fetcher(url, options); },
  });
  return { control, panel, elements, storage, calls, el: id => elements.get('#' + id) };
}
const response = data => ({ ok: true, json: async () => data });

test('F24-1 单本/批次/修复创建后关闭页面仍能恢复，旧已完成记录兼容', () => {
  const native = nativeStorage(); const first = later.safeStorage({ localStorage: native });
  library.saveHistory(first, { jobId: 'legacy', filename: 'old.epub', downloadUrl: '/expired?sig=old' });
  for (const kind of ['job', 'batch', 'repair']) library.saveHistory(first, { kind, jobId: kind + '_one', status: 'pending_payment', filename: kind });
  const reloaded = later.safeStorage({ localStorage: native });
  assert.equal(library.loadHistory(reloaded).length, 4);
  assert.equal(later.taskLink(library.loadHistory(reloaded)[0]), '/epub-repair.html?job_id=repair_one');
  assert.equal(later.taskLink({ kind: 'batch', id: 'batch_one' }), '/?batch_id=batch_one');
});

test('F24-2 fragment 令牌按单本/批次导入后立刻从地址移除，不吞 OAuth 登录 token', () => {
  for (const query of ['?job_id=book', '?batch_id=batch']) {
    const saved = []; let url;
    later.importFragment({ search: query, pathname: '/', hash: '#access_token=private%2Btoken' }, { replaceState(a, b, value) { url = value; } }, (...args) => saved.push(args), false);
    assert.equal(saved[0][1], 'private+token'); assert.equal(url, '/' + query);
    assert.equal(saved[0][0], query.includes('batch') ? 'batch:batch' : 'book');
  }
  later.importFragment({ search: '', pathname: '/', hash: '#access_token=oauth' }, { replaceState() { throw Error('OAuth token must remain'); } }, () => {}, false);
});

test('F24-3 localStorage 禁用或配额满时，当前任务 token/状态仍在内存可用', () => {
  for (const host of [{ get localStorage() { throw Error('SecurityError'); } }, { localStorage: { getItem: () => null, setItem() { throw Error('QuotaExceeded'); }, removeItem() {} } }]) {
    const storage = later.safeStorage(host); storage.setItem('token', 'secret');
    assert.equal(storage.getItem('token'), 'secret'); assert.equal(storage.available, false);
    library.saveHistory(storage, { jobId: 'book', kind: 'job' }); assert.equal(library.loadHistory(storage).length, 1);
  }
});

test('F24-4 已过期下载链接不进入页面，恶意文件名只写 textContent', async () => {
  const h = harness(() => response({ available: true }));
  library.saveHistory(h.storage, { jobId: 'book', filename: '<img src=x onerror=alert(1)>', downloadUrl: 'javascript:alert(1)' });
  h.control.render();
  const link = h.el('returnLaterTaskList').children[0].children[0];
  assert.equal(link.href, '/?job_id=book');
  assert.equal(link.textContent, '<img src=x onerror=alert(1)>');
  assert.ok(!h.panel.innerHTML.includes('onerror'));
  assert.equal(later.taskLink({ kind: 'job', id: 'x" onmouseover="bad' }), '');
  await tick();
});

test('F24-5 可选邮箱在新任务创建后异步自动保存，使用对应访问令牌', async () => {
  const h = harness((url, options) => response(url.includes('capabilities') ? { available: true } : { enabled: true, email: 'reader@example.com' }));
  h.el('notificationEmail').value = 'reader@example.com';
  await h.control.setTask({ kind: 'batch', id: 'batch1', filename: 'two books' }, true);
  const put = h.calls.find(call => call.method === 'PUT');
  assert.equal(put.url, '/api/v2/batches/batch1/notification-email');
  assert.equal(put.headers['X-Job-Token'], 'token:batch:batch1');
  assert.deepEqual(JSON.parse(put.body), { email: 'reader@example.com' });
  assert.ok(h.el('notificationEmailHint').textContent.includes('邮箱已保存'));
});

test('F24-6 邮件未配置时不能误报已订阅，不影响任务记录保存', async () => {
  const h = harness(() => response({ available: false }));
  h.el('notificationEmail').value = 'reader@example.com';
  await h.control.setTask({ kind: 'job', id: 'book' }, true);
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 0);
  assert.ok(h.el('notificationEmailHint').textContent.includes('尚未启用'));
  assert.equal(library.loadHistory(h.storage)[0].jobId, 'book');
});

test('F24-7 保存邮箱失败只提示重试，清空保存取消通知', async () => {
  let fail = false;
  const h = harness((url, options) => {
    if (url.includes('capabilities')) return response({ available: true });
    if (options.method === 'PUT' && fail) throw Error('offline');
    return response({ enabled: false });
  });
  await tick(); await h.control.setTask({ kind: 'repair', id: 'repair1' });
  h.el('notificationEmail').value = ''; await h.control.save();
  assert.deepEqual(JSON.parse(h.calls.find(call => call.method === 'PUT').body), { email: '' });
  fail = true; h.el('notificationEmail').value = 'reader@example.com'; await h.control.save();
  assert.ok(h.el('notificationEmailHint').textContent.includes('未保存成功'));
  assert.equal(library.loadHistory(h.storage)[0].jobId, 'repair1');
});

test('F24-8 服务端邮箱/文件名作为数据呈现，不插入 HTML', async () => {
  const address = '\"><img src=x onerror=alert(1)>';
  const h = harness(url => response(url.includes('capabilities') ? { available: true } : { email: address, enabled: true }));
  await tick(); await h.control.setTask({ kind: 'job', id: 'book' });
  assert.equal(h.el('notificationEmail').value, address);
  assert.ok(!h.panel.innerHTML.includes(address));
});

test('F24-9 首页和修复页面均挂载全局提示并在创建时保留任务', () => {
  for (const html of [index, repair]) {
    assert.ok(html.includes('id="returnLaterPanel"'));
    assert.ok(html.includes('FixEpubReturnLater.mount'));
    assert.ok(!html.includes('请勿关闭页面'));
    for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
      if (!match[0].includes('application/ld+json')) new vm.Script(match[1]);
    }
  }
  assert.ok(index.includes('rememberPage(isBatchMode'));
  assert.ok(repair.includes('rememberRepairPage(currentJobId)'));
  assert.ok(repair.includes('reportBlock.classList.add(\'visible\')'));
});

test('F24-10 损坏或异常历史内容不会中断页面初始化', () => {
  const storage = nativeStorage();
  for (const value of ['{}', 'null', '"bad"', '[null,1,"bad"]']) {
    storage.setItem('epub_factory_history', value);
    assert.deepEqual(library.loadHistory(storage), []);
  }
});

test('F24-11 邮件服务停用后仍能清空取消原订阅', async () => {
  const h = harness((url, options) => {
    if (url.includes('capabilities')) return response({ available: false });
    return response(options.method === 'PUT' ? { email: '', enabled: false } : { email: 'old@example.com', enabled: true });
  });
  await tick(); await h.control.setTask({ kind: 'job', id: 'book' });
  assert.equal(h.el('saveNotificationEmail').disabled, false);
  assert.equal(h.el('notificationEmail').value, 'old@example.com');
  h.el('notificationEmail').value = '';
  await h.control.save();
  const put = h.calls.find(call => call.method === 'PUT');
  assert.deepEqual(JSON.parse(put.body), { email: '' });
  assert.ok(h.el('notificationEmailHint').textContent.includes('未设置邮件通知'));
});

test('F24-12 重新选择文件或修复另一文件不会丢失上传前填写的邮箱', async () => {
  const h = harness(url => response(url.includes('capabilities') ? { available: true } : { enabled: true, email: 'before-upload@example.com' }));
  await tick();
  h.el('notificationEmail').value = 'before-upload@example.com';
  await h.control.setTask(null, true);
  assert.equal(h.el('notificationEmail').value, 'before-upload@example.com');
  await h.control.setTask({ kind: 'repair', id: 'newrepair' }, true);
  assert.equal(JSON.parse(h.calls.find(call => call.method === 'PUT').body).email, 'before-upload@example.com');
});

test('F24-13 处理页不加载第三方统计且不对外泄露任务链接 Referrer', () => {
  for (const html of [index, repair]) {
    assert.ok(html.includes('<meta name="referrer" content="no-referrer"'));
    assert.ok(!html.includes('googletagmanager.com'));
    assert.ok(!html.includes('hm.baidu.com'));
    assert.ok(!html.includes('处理完成后自动删除'));
  }
  assert.ok(index.includes('reportCheckoutEvent("payment_clicked")'));
});

test('F24-14 上一任务邮箱保存中创建新任务，仍会保存至新任务而非静默跳过', async () => {
  let finishFirst;
  const h = harness((url, options) => {
    if (url.includes('capabilities')) return response({ available: true });
    if (options.method === 'PUT' && url.includes('/first/')) return new Promise(resolve => { finishFirst = () => resolve(response({ enabled: true })); });
    return response({ enabled: true });
  });
  await tick();
  h.el('notificationEmail').value = 'first@example.com';
  const first = h.control.setTask({ kind: 'job', id: 'first' }, true);
  await tick();
  h.el('notificationEmail').value = 'second@example.com';
  const second = h.control.setTask({ kind: 'job', id: 'second' }, true);
  await tick(); finishFirst(); await Promise.all([first, second]);
  const writes = h.calls.filter(call => call.method === 'PUT');
  assert.equal(writes.length, 2);
  assert.ok(writes[1].url.includes('/second/'));
  assert.equal(JSON.parse(writes[1].body).email, 'second@example.com');
});

test('F24-15 排队保存期间切换查看另一任务，不会修改或取消所查看任务的邮箱', async () => {
  let finishFirst;
  const h = harness((url, options) => {
    if (url.includes('capabilities')) return response({ available: true });
    if (options.method === 'PUT' && url.includes('/first/')) return new Promise(resolve => { finishFirst = () => resolve(response({ enabled: true })); });
    return response({ enabled: true, email: 'viewed@example.com' });
  });
  await tick();
  h.el('notificationEmail').value = 'first@example.com';
  const first = h.control.setTask({ kind: 'job', id: 'first' }, true);
  await tick();
  h.el('notificationEmail').value = 'second@example.com';
  const second = h.control.setTask({ kind: 'job', id: 'second' }, true);
  await tick();
  await h.control.setTask({ kind: 'job', id: 'viewed' });
  finishFirst(); await Promise.all([first, second]);
  const writes = h.calls.filter(call => call.method === 'PUT');
  assert.equal(writes.length, 1);
  assert.ok(writes[0].url.includes('/first/'));
  assert.equal(h.el('notificationEmail').value, 'viewed@example.com');
});
