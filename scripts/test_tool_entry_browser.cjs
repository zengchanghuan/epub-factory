/* R11 browser gate: real HTML/JS over loopback HTTP, synthetic API/payment only.
 * Node >=22 and Chrome/Chromium required. No npm packages, account or API keys.
 * Run: node scripts/test_tool_entry_browser.cjs
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { createHash } = require('node:crypto');
const root = path.resolve(__dirname, '../frontend');
const chromeBin = process.env.CHROME_BIN || (process.platform === 'darwin'
  ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : 'google-chrome');
const runtime = fs.mkdtempSync(path.join(os.tmpdir(), 'fixepub-r11-browser-'));
const artifact = Buffer.from('R11 browser fixture; real historical EPUB validation is D49.');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const requests = [], jobs = new Map(), exceptions = [], denied = [];
let creates = 0, confirms = 0, continues = 0, cdp, chrome, server, origin, passed = 0;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

function detail(job) {
  return {
    job_id: job.id, source_filename: 'browser-fixture.epub', status: job.status,
    enable_translation: job.translate, output_mode: 'simplified', message: job.status, amount: job.translate ? '3.99' : '0.99',
    ...(job.status === 'awaiting_confirmation' ? { translation_preflight: { resolved_strategy: 'neutral_faithful', glossary: {}, chapters: [] } } : {}),
    ...(job.status === 'completed' ? { download_url: '/fixture-download/' + job.id + '?sig=current' } : {}),
  };
}
function json(res, data, status = 200) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(data));
}
async function serve(req, res) {
  const url = new URL(req.url, origin);
  requests.push({ method: req.method, path: url.pathname, token: req.headers['x-job-token'] });
  if (url.pathname === '/api/v2/jobs' && req.method === 'POST') {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const form = await new Request(origin, { method: 'POST', headers: req.headers, body: Buffer.concat(chunks) }).formData();
    const fields = Object.fromEntries([...form].filter(([, value]) => typeof value === 'string'));
    const id = 'fixture' + (++creates), translate = fields.enable_translation === 'true';
    const job = { id, translate, fields, token: 'secret-' + id, status: translate ? 'awaiting_confirmation' : 'pending_payment' };
    jobs.set(id, job);
    return json(res, { ...detail(job), access_token: job.token, amount: '0.99', qr_code: 'offline-fixture' });
  }
  if (url.pathname === '/api/v2/jobs') return json(res, { items: [...jobs.values()].map(detail) });
  if (url.pathname === '/api/v2/email-capabilities') return json(res, { available: false });
  if (url.pathname === '/api/v2/track/pv') return json(res, {});
  if (url.pathname.startsWith('/api/v2/batches/historybatch')) {
    if (req.headers['x-job-token'] !== 'batch-secret') return json(res, {}, 403);
    if (url.pathname.endsWith('/continue-payment') && req.method === 'POST') {
      continues++;
      return json(res, { batch_id: 'historybatch', status: 'pending_payment', file_count: 2, counts: {}, jobs: [],
        amount: '1.98', checkout_available: true, pay_url: origin + '/fake-payment/batch_historybatch' });
    }
    return json(res, { batch_id: 'historybatch', status: 'pending_payment', file_count: 2, counts: {}, jobs: [] });
  }
  const match = url.pathname.match(/^\/api\/v2\/jobs\/([^/]+)(.*)$/);
  if (match) {
    const job = jobs.get(match[1]);
    if (!job || req.headers['x-job-token'] !== job.token) return json(res, { detail: 'Denied' }, 403);
    if (match[2] === '/confirm-profile') {
      confirms++; job.status = 'pending_payment';
      return json(res, { ...detail(job), amount: '3.99', pay_url: origin + '/fake-payment' });
    }
    if (match[2] === '/continue-payment' && req.method === 'POST') {
      continues++;
      await pause(80);
      if (job.checkoutError) return json(res, { detail: '暂时无法核验支付状态，请稍后重试；请勿重复付款' }, 503);
      if (job.paidDuringContinue) job.status = 'running';
      return json(res, { ...detail(job), checkout_available: job.status === 'pending_payment',
        pay_url: job.status === 'pending_payment' && !job.originalQr ? origin + '/fake-payment/' + job.id : null,
        qr_code: job.status === 'pending_payment' && job.originalQr ? origin + '/original-qr/' + job.id : null });
    }
    if (match[2] === '/cancel') job.status = 'cancelled';
    if (match[2] === '/events') return json(res, { items: [] });
    if (match[2] === '/notification-email') return json(res, { enabled: false });
    if (match[2] === '/checkout-events') return json(res, {});
    if (!['', '/recover', '/cancel'].includes(match[2])) return json(res, { detail: 'Unexpected endpoint' }, 404);
    // Deliberately late status response: default initialization must not reapply an entry preset.
    if (!match[2]) await pause(80);
    return json(res, detail(job));
  }
  if (url.pathname.startsWith('/fixture-download/')) {
    const job = jobs.get(url.pathname.split('/').pop());
    if (!job || job.status !== 'completed' || url.searchParams.get('token') !== job.token) return json(res, {}, 403);
    res.writeHead(200, { 'Content-Type': 'application/epub+zip', 'Content-Disposition': 'attachment; filename="' + job.id + '.epub"' });
    return res.end(artifact);
  }
  if (url.pathname.startsWith('/api/') || req.method !== 'GET') return json(res, { detail: 'Unexpected request' }, 404);
  const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404); return res.end(); }
  const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }[path.extname(file)] || 'application/octet-stream';
  res.writeHead(200, { 'Content-Type': mime + '; charset=utf-8', 'Cache-Control': 'no-store' });
  fs.createReadStream(file).pipe(res);
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url), client = new CDP(ws);
    ws.addEventListener('message', event => {
      const data = JSON.parse(event.data);
      if (data.id) {
        const pending = client.pending.get(data.id); if (!pending) return;
        client.pending.delete(data.id); clearTimeout(pending.timer);
        if (data.error) pending.reject(new Error(JSON.stringify(data.error))); else pending.resolve(data.result);
      } else for (const fn of client.handlers.get(data.method) || []) fn(data.params);
    });
    await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
    return client;
  }
  on(event, fn) { this.handlers.set(event, [...this.handlers.get(event) || [], fn]); }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 10000);
      this.pending.set(id, { resolve, reject, timer }); this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}
async function evaluate(expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  return result.result.value;
}
async function until(expression) {
  for (let i = 0; i < 120; i++) {
    try { if (await evaluate(expression)) return; } catch (_) { /* Navigation destroys old context. */ }
    await pause(50);
  }
  throw new Error('Browser condition timeout: ' + expression + '\n' + await evaluate('document.body.innerText.slice(0, 2000)'));
}
async function navigate(url) {
  // Force a document load even when the next fixture differs only by fragment;
  // otherwise Chrome treats it as in-page navigation, not a refresh.
  await cdp.send('Page.navigate', { url: 'about:blank' });
  await until('location.href === "about:blank"');
  await cdp.send('Page.navigate', { url: origin + url });
  await until('location.origin === ' + JSON.stringify(origin) + ' && document.readyState === "complete" && !!document.body');
}
async function status(value) { await until(`document.getElementById('statusMessage')?.textContent === ${JSON.stringify(value)}`); }
function pass(name) { passed++; console.log('PASS ' + name); }

async function main() {
  server = http.createServer((req, res) => serve(req, res).catch(error => { console.error(error); json(res, { error: error.message }, 500); }));
  await new Promise((resolve, reject) => { server.on('error', reject); server.listen(0, '127.0.0.1', resolve); });
  origin = 'http://127.0.0.1:' + server.address().port;
  chrome = spawn(chromeBin, ['--headless=new', '--remote-debugging-port=0', '--user-data-dir=' + path.join(runtime, 'profile'),
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-first-run', '--no-default-browser-check',
    '--disable-default-apps', '--metrics-recording-only', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const endpoint = await new Promise((resolve, reject) => {
    let stderr = ''; const timer = setTimeout(() => reject(new Error('Chrome startup timeout: ' + stderr)), 15000);
    chrome.on('error', error => { clearTimeout(timer); reject(error); });
    chrome.on('exit', code => { clearTimeout(timer); reject(new Error('Chrome exited: ' + code + '\n' + stderr)); });
    chrome.stderr.on('data', chunk => { stderr += chunk; const found = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (found) { clearTimeout(timer); resolve(found[1]); } });
  });
  const debug = new URL(endpoint);
  const tab = await (await fetch('http://' + debug.host + '/json/new?about:blank', { method: 'PUT' })).json();
  cdp = await CDP.connect(tab.webSocketDebuggerUrl);
  cdp.on('Runtime.exceptionThrown', event => exceptions.push(event.exceptionDetails.exception?.description || event.exceptionDetails.text));
  cdp.on('Fetch.requestPaused', event => {
    const url = event.request.url;
    const allowed = url.startsWith(origin + '/') || url.startsWith('data:');
    if (!allowed) denied.push(url);
    cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', allowed ? { requestId: event.requestId } : { requestId: event.requestId, errorReason: 'BlockedByClient' }).catch(() => {});
  });
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable');
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
  await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `window.EPUB_FACTORY_API = ${JSON.stringify(origin)};` });
  await cdp.send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: runtime });

  for (const [page, tool] of [['', ''], ['epub-translator.html', 'translate'], ['vertical-to-horizontal.html', 'horizontal'], ['traditional-to-simplified.html', 'simplified']]) {
    const before = creates;
    await navigate('/' + page);
    if (page) {
      assert.equal(await evaluate('document.querySelectorAll("input[type=file]").length'), 0);
      await cdp.send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
      const screenshot = await cdp.send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(runtime, page + '.png'), Buffer.from(screenshot.data, 'base64'));
      await cdp.send('Emulation.clearDeviceMetricsOverride');
      await evaluate('document.querySelector("[data-entry-link=upload]").click()');
      await until('location.pathname === "/" && !!document.getElementById("submitBtn") && document.readyState === "complete"');
    }
    assert.equal(creates, before);
    assert.equal(await evaluate('document.querySelector("input[name=taskMode]:checked").value'), tool === 'translate' ? 'translate' : 'convert');
    assert.equal(await evaluate('document.getElementById("conversionMode").value'), tool === 'simplified' ? 'simplified|auto' : 'simplified|tw');
    assert.equal(await evaluate('location.search'), '');
    await evaluate(`(() => { const files = new DataTransfer(); files.items.add(new File(['synthetic browser upload'], 'browser-fixture.epub', {type:'application/epub+zip'})); const input = document.getElementById('fileInput'); input.files = files.files; input.dispatchEvent(new Event('change', {bubbles:true})); document.getElementById('submitBtn').click(); })()`);
    await until('location.search.includes("job_id=")');
    const id = await evaluate('new URLSearchParams(location.search).get("job_id")');
    const job = jobs.get(id);
    assert.equal(creates, before + 1);
    assert.equal(job.fields.enable_translation, tool === 'translate' ? 'true' : 'false');
    assert.equal(job.fields.traditional_variant, tool === 'simplified' ? 'auto' : 'tw');
    assert.equal(job.fields.enable_precision_polish, 'false');
    if (tool === 'translate') {
      await until('document.getElementById("translationPreflightPanel").style.display === "block"');
      assert.equal(job.fields.translation_model, 'deepseek-flash');
      assert.equal(job.fields.profile_confirmation, 'true');
      await evaluate('document.getElementById("confirmPreflightBtn").click()');
    }
    await status('pending_payment');
    assert.equal(await evaluate('document.getElementById("resultActions").classList.contains("visible")'), false);
    assert.equal(await evaluate('document.getElementById("statusBadge").classList.contains("running")'), false);
    pass((page || 'index.html') + ': native entry → actual upload/confirm → pending, no premature delivery');

    // Refresh pending, paid/running, and completed states through the old page URL.
    for (const state of ['pending_payment', 'running', 'completed']) {
      job.status = state;
      await navigate('/' + page + '?job_id=' + id + '&tool=simplified#access_token=' + job.token);
      await status(state);
      assert.equal(await evaluate('location.pathname'), '/');
      assert.equal(await evaluate('location.hash'), '');
      assert.equal(creates, before + 1);
      assert.equal(await evaluate('JSON.parse(localStorage.getItem("job_access_tokens_v1"))[' + JSON.stringify(id) + ']'), job.token);
      assert.equal(await evaluate('document.getElementById("resultActions").classList.contains("visible")'), state === 'completed');
    }
    const url = await evaluate('document.getElementById("downloadBtn").href');
    assert.equal(new URL(url).searchParams.get('token'), job.token);
    await evaluate('document.getElementById("downloadBtn").click()');
    for (let i = 0; i < 120 && !fs.existsSync(path.join(runtime, id + '.epub')); i++) await pause(50);
    assert.equal(sha(fs.readFileSync(path.join(runtime, id + '.epub'))), sha(artifact));
    pass((page || 'index.html') + ': pending/paid/completed refresh, authorized browser download SHA');

    for (const terminal of ['cancelled', 'qa_failed', 'partial_completed']) {
      job.status = terminal;
      await navigate('/' + page + '?job_id=' + id + '#access_token=' + job.token);
      await status(terminal);
      assert.equal(await evaluate('document.getElementById("resultActions").classList.contains("visible")'), false);
      assert.equal(creates, before + 1);
    }
    pass((page || 'index.html') + ': cancelled/QA-failed/partial restore never delivers or resubmits');
  }
  // All three task-center links preserve native navigation with no task creation.
  for (const page of ['epub-translator.html', 'vertical-to-horizontal.html', 'traditional-to-simplified.html']) {
    await navigate('/' + page);
    await evaluate('document.querySelector("[data-entry-link=tasks]").click()');
    await until('document.getElementById("tasksCard")?.classList.contains("visible")');
    assert.equal(creates, 4);
    pass(page + ': task-center CTA');
  }
  await navigate('/epub-translator.html?job_id=ignored&batch_id=historybatch#access_token=batch-secret');
  await until('document.getElementById("statusMessage")?.textContent === "等待整批订单支付"');
  assert.equal(await evaluate('document.getElementById("metaJobId").textContent'), '批次 historybatch');
  assert.equal(await evaluate('JSON.parse(localStorage.getItem("job_access_tokens_v1"))["batch:historybatch"]'), 'batch-secret');
  pass('legacy batch link outranks job/mode, fragment authorization survives');

  const restoreScript = await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `document.addEventListener('DOMContentLoaded', () => { const radio = document.querySelector('input[name=taskMode][value=translate]'); if (radio) radio.checked = true; }, {once:true});` });
  await navigate('/?tool=simplified');
  assert.equal(await evaluate('document.querySelector("input[name=taskMode]:checked").value'), 'translate');
  await cdp.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: restoreScript.identifier });
  await evaluate(`document.querySelector('input[name=taskMode][value=convert]').click(); window.dispatchEvent(new Event('pageshow')); location.hash = '#return';`);
  assert.equal(await evaluate('document.querySelector("input[name=taskMode]:checked").value'), 'convert');
  pass('restored form beats new preset; pageshow/hash change never reapplies it');
  const translationJob = [...jobs.values()].find(job => job.translate);
  translationJob.status = 'running';
  await navigate('/epub-translator.html?job_id=' + translationJob.id);
  await status('running');
  await evaluate('document.getElementById("cancelJobBtn").click()');
  await status('cancelled');
  assert.equal(creates, 4);
  assert.equal(await evaluate('document.getElementById("resultActions").classList.contains("visible")'), false);
  pass('legacy translation task: real stop button reaches shared cancel endpoint');

  // Continue checkout uses the rendered app and real event handlers, but only
  // this loopback synthetic gateway; no payment page or customer order is used.
  translationJob.status = 'pending_payment';
  await navigate('/?job_id=' + translationJob.id);
  await status('pending_payment');
  await until('document.getElementById("continuePaymentPanel").style.display === "block"');
  const beforeContinue = continues;
  await pause(100);
  assert.equal(continues, beforeContinue); // Polling must not recreate checkout.
  await evaluate('document.getElementById("continuePaymentBtn").click(); document.getElementById("continuePaymentBtn").click();');
  await until('!document.getElementById("continuePaymentLink").hidden');
  assert.equal(continues, beforeContinue + 1);
  assert.equal(creates, 4);
  assert.equal(await evaluate('document.getElementById("continuePaymentLink").href'), origin + '/fake-payment/' + translationJob.id);
  pass('pending refresh: explicit continue, duplicate click suppressed, original order reused');

  await evaluate('document.getElementById("navTasks").click()');
  await until('document.querySelector(".tasks-continue-payment-btn") !== null');
  assert.equal(await evaluate('document.getElementById("continuePaymentPanel").getBoundingClientRect().height > 0'), true);
  const mainUrl = await evaluate('location.href');
  await evaluate('window.checkoutPopupCalls=[]; window.open=(...args)=>{ checkoutPopupCalls.push(args); return {closed:false,focus(){},close(){this.closed=true}}; }; document.getElementById("continuePaymentLink").click();');
  assert.equal(await evaluate('location.href'), mainUrl);
  assert.equal(await evaluate('checkoutPopupCalls.length'), 1);
  assert.equal(await evaluate('checkoutPopupCalls[0][1]'), 'fixepub-alipay');
  assert((await evaluate('checkoutPopupCalls[0][2]')).includes('width=520'));
  const paymentScreenshot = await cdp.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(runtime, 'continue-payment.png'), Buffer.from(paymentScreenshot.data, 'base64'));
  pass('task-center checkout remains visible and opens the existing small window, main page unchanged');

  translationJob.checkoutError = true;
  await evaluate('document.getElementById("continuePaymentBtn").click()');
  await until('document.getElementById("continuePaymentMessage").textContent.includes("无法核验")');
  assert.equal(await evaluate('document.getElementById("continuePaymentLink").hidden'), true);
  assert.equal(await evaluate('document.getElementById("continuePaymentLink").getAttribute("href")'), '#');
  assert.equal(creates, 4);
  pass('unknown gateway state clears all old payment links and never recreates an order');

  translationJob.checkoutError = false;
  translationJob.paidDuringContinue = true;
  await evaluate('document.getElementById("continuePaymentBtn").click()');
  await status('running');
  assert.equal(await evaluate('document.getElementById("continuePaymentLink").hidden'), true);
  assert.equal(await evaluate('document.getElementById("continuePaymentPanel").style.display'), 'none');
  assert.equal(creates, 4);
  pass('payment discovered during continue returns to progress without another payment link');

  await navigate('/?batch_id=historybatch#access_token=batch-secret');
  await until('document.getElementById("continuePaymentPanel").style.display === "block"');
  await evaluate('document.getElementById("continuePaymentBtn").click()');
  await until('!document.getElementById("continuePaymentLink").hidden');
  assert.equal(await evaluate('document.getElementById("continuePaymentLink").href'), origin + '/fake-payment/batch_historybatch');
  assert.equal(creates, 4);
  pass('batch refresh continues only the original aggregate checkout with its batch token');

  const qrJob = [...jobs.values()].find(job => !job.translate);
  qrJob.status = 'pending_payment'; qrJob.originalQr = true;
  await navigate('/?job_id=' + qrJob.id);
  await status('pending_payment');
  await evaluate('document.getElementById("continuePaymentBtn").click()');
  await until('!document.getElementById("continuePaymentQr").hidden && document.getElementById("continuePaymentQr").naturalWidth > 0');
  assert.equal(await evaluate('document.getElementById("continuePaymentLink").hidden'), true);
  assert((await evaluate('document.getElementById("continuePaymentQr").src')).startsWith('data:image/png;base64,'));
  assert(!requests.some(request => request.path.startsWith('/original-qr/')));
  const qrScreenshot = await cdp.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(runtime, 'continue-payment-qr.png'), Buffer.from(qrScreenshot.data, 'base64'));
  pass('original QR is rendered by the local library without switching product or opening a payment URL');
  qrJob.checkoutError = true;
  await evaluate('document.getElementById("continuePaymentBtn").click()');
  await until('document.getElementById("continuePaymentMessage").textContent.includes("无法核验")');
  assert.equal(await evaluate('document.getElementById("continuePaymentQr").hidden'), true);
  assert.equal(await evaluate('document.getElementById("continuePaymentQr").getAttribute("src")'), null);
  assert.equal(creates, 4);
  pass('unverifiable QR continuation clears the previously rendered code');
  assert.equal(confirms, 1);
  assert.deepEqual(exceptions, []);
  assert(requests.every(request => !/paypal|create-order|capture-order/.test(request.path)));
  assert(requests.filter(request => /\/recover$/.test(request.path)).every(request => request.token));
  console.log(JSON.stringify({ passed, creates, confirms, continues, jsExceptions: exceptions.length, blockedExternalPageRequests: denied.length, runtime }, null, 2));
}

main().catch(error => { console.error(error); console.error({ exceptions, lastRequests: requests.slice(-10) }); process.exitCode = 1; }).finally(async () => {
  if (cdp) cdp.ws.close();
  if (chrome) { chrome.kill('SIGTERM'); await pause(300); if (chrome.exitCode === null) chrome.kill('SIGKILL'); }
  if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
  // Keep only synthetic artifacts/log evidence. The disposable Chrome profile is never reused.
  fs.rmSync(path.join(runtime, 'profile'), { recursive: true, force: true, maxRetries: 3 });
});
