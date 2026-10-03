/* Real PDF UI/client functions with controlled DOM and HTTP; no live payment. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const pdf = require('../pdf-conversion.js');
const lib = require('../lib.js');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const capPayload = { pdf_text_conversion: { enabled: true, preserves_original: true, price_cny: '0.99', max_file_size_mb: 20, max_pages: 500 } };
const cap = pdf.capability(capPayload);
const file = { name: 'selected.pdf', size: 1234 };
const warnings = ['empty_password_encryption', 'pages_without_extractable_text', 'late_text_overlay_preserved'];
const prepared = (extra = {}, jobId = 'job-a') => ({ job_id: jobId, status: 'awaiting_confirmation',
  pdf_conversion: { phase: 'prepared', plan_id: 'a'.repeat(64), amount: '0.99', page_count: 346,
    normalized_characters: 167334, image_assets: 110, toc_entries: 28, warnings: [...warnings],
    can_confirm: true, confirmed: false, preserves_original: true, ...extra } });
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
class FormDataStub { constructor() { this.fields = []; } append(...entry) { this.fields.push(entry); } }
function client(respond) {
  const calls = [];
  return { calls, api: pdf.createClient({ api: '', FormData: FormDataStub,
    fetch: async (url, options) => { calls.push({ url, options }); return respond(url, options); },
    headers: () => ({ 'X-Client-Session': 'client' }), authHeaders: id => ({ 'X-Job-Token': id }), adminKey: () => 'test-key' }) };
}

test('PDF capability is server-gated and fails closed for missing, false or malformed values', () => {
  assert.equal(cap.enabled, true); assert.equal(cap.price, '0.99'); assert.equal(cap.maxBytes, 20 * 1024 * 1024);
  for (const patch of [{ enabled: false }, { enabled: 'true' }, { preserves_original: false }, { price_cny: '<script>' },
    { price_cny: -1 }, { max_file_size_mb: 0 }, { max_pages: 1.5 }]) {
    assert.equal(pdf.capability({ pdf_text_conversion: { ...capPayload.pdf_text_conversion, ...patch } }).enabled, false);
  }
  assert.equal(pdf.capability({}).enabled, false);
});

test('PDF accepts one nonempty case-insensitive PDF within server limit, never batches/folders/generic formats', () => {
  assert.equal(pdf.validateFiles([file], cap), '');
  assert.equal(pdf.validateFiles([{ ...file, name: 'BOOK.PDF', size: cap.maxBytes }], cap), '');
  for (const files of [[], [file, file], [{ ...file, name: 'book.epub' }], [{ ...file, size: 0 }], [{ ...file, size: cap.maxBytes + 1 }]]) {
    assert(pdf.validateFiles(files, cap));
  }
  assert(pdf.validateFiles([file], { enabled: false }));
  assert.equal(lib.validateFile('book.pdf').valid, false);
  assert(!lib.SUPPORTED_EXTENSIONS.includes('.pdf'));
});

test('PDF mode and task status say preserve original, never infer traditional-to-simplified', () => {
  assert.equal(lib.formatJobMeta({ output_mode: 'original' }), 'PDF → EPUB · 保留原文');
  assert.equal(lib.mapV2StatusText('awaiting_confirmation', false, true), '待确认 PDF 转换');
  assert.equal(lib.mapV2StatusText('awaiting_confirmation', true), '待确认画像');
});

test('Frozen plan amount and all exact warning codes are required for confirmation', () => {
  const data = prepared({ amount: '1.49' });
  assert.equal(pdf.view(data).amount, '1.49');
  assert.deepStrictEqual(pdf.confirmationBody(data, [...warnings].reverse()), { plan_id: 'a'.repeat(64), accepted_warnings: warnings });
  for (const accepted of [[], warnings.slice(1), [...warnings, 'other'], [...warnings, warnings[0]], 'all']) {
    assert.throws(() => pdf.confirmationBody(data, accepted));
  }
  assert.deepStrictEqual(pdf.confirmationBody(prepared({ warnings: [] }), []), { plan_id: 'a'.repeat(64), accepted_warnings: [] });
});

test('Unknown warnings, malformed plans, unsafe amounts, blocked stages and nonboolean grants cannot create orders', () => {
  for (const extra of [{ warnings: ['future_warning'] }, { warnings: ['constructor'] }, { warnings: ['<img>'] },
    { warnings: null }, { warnings: [...warnings, warnings[0]] }, { warnings: ['memory_limit_unavailable'] },
    { warnings: ['epubcheck_warnings'] }, { phase: 'preparing' }, { phase: 'confirmed' },
    { confirmed: true }, { can_confirm: 'true' }, { can_confirm: false }, { amount: 'NaN' }, { plan_id: '/private/path' },
    { plan_id: 'a' }, { plan_id: 'A'.repeat(64) }, { blocked_reason: 'review_blocked' }]) {
    assert.equal(pdf.view(prepared(extra)).canConfirm, false);
  }
  assert.equal(pdf.view({ ...prepared(), status: 'pending_payment' }).canConfirm, false);
  assert.equal(pdf.view({}), null);
  assert(!pdf.view(prepared({ can_confirm: false, blocked_reason: '<script>/secret</script>' })).blockedMessage.includes('secret'));
});

test('Capability fetch failure, invalid JSON and disabled response never reveal upload', async () => {
  for (const answer of [response({}, 503), response({}), { ok: true, json: async () => { throw Error('invalid'); } }]) {
    const c = client(async () => answer); assert.equal((await c.api.capabilities()).enabled, false);
    assert.equal(c.calls[0].options.cache, 'no-store');
  }
});

test('Dedicated upload transport sends only file and optional admin key, never AI/batch fields', async () => {
  const c = client(async () => response({ job_id: 'job-a', access_token: 'token', status: 'queued' }));
  await c.api.create([file], cap);
  assert.equal(c.calls[0].url, '/api/v2/pdf-jobs');
  assert.deepStrictEqual(c.calls[0].options.body.fields, [['file', file], ['admin_key', 'test-key']]);
  assert.deepStrictEqual(c.calls[0].options.headers, { 'X-Client-Session': 'client' });
  await assert.rejects(c.api.create([file, file], cap)); assert.equal(c.calls.length, 1);
});

test('Double upload/confirmation click does not create a second HTTP request', async () => {
  const gate = deferred(), c = client(() => gate.promise);
  const first = c.api.create([file], cap);
  await assert.rejects(c.api.create([file], cap), /重复/); assert.equal(c.calls.length, 1);
  gate.resolve(response({ job_id: 'job-a' })); await first;
  const confirmation = deferred(), d = client(() => confirmation.promise);
  const one = d.api.confirm('job-a', prepared(), warnings);
  await assert.rejects(d.api.confirm('job-a', prepared(), warnings), /重复/); assert.equal(d.calls.length, 1);
  confirmation.resolve(response({ job_id: 'job-a', status: 'pending_payment' })); await one;
});

test('Confirmation transport sends token and exact plan/warnings without overrides or raw PDF data', async () => {
  const c = client(async () => response({ job_id: 'job-a', status: 'pending_payment' }));
  await c.api.confirm('job-a', prepared(), warnings);
  assert.equal(c.calls[0].url, '/api/v2/jobs/job-a/confirm-conversion');
  assert.equal(c.calls[0].options.headers['X-Job-Token'], 'job-a');
  assert.deepStrictEqual(JSON.parse(c.calls[0].options.body), pdf.confirmationBody(prepared(), warnings));
  await assert.rejects(c.api.confirm('../invalid', prepared(), warnings)); assert.equal(c.calls.length, 1);
});

test('409 stale plans and 503 service-off give fixed messages, no leaked backend path/HTML', async () => {
  for (const status of [409, 503, 500]) {
    const c = client(async () => response({ detail: '/private/secret/<script>text</script>' }, status));
    await assert.rejects(c.api.confirm('job-a', prepared(), warnings), e => !e.message.includes('secret') && !e.message.includes('<'));
  }
});

class Node {
  constructor() { this.children = []; this.style = {}; this.dataset = {}; this.listeners = {}; this.disabled = false; this.hidden = true; this.checked = false; this.files = []; this.textContent = ''; this.classList = { contains: () => false }; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll(selector) { const all = this.children.flatMap(c => c instanceof Node ? [c, ...c.querySelectorAll('all')] : []); return all.filter(n => n.type === 'checkbox' && (selector !== 'input:checked' || n.checked)); }
  click() { this.clicked = true; }
  set value(value) { this._value = value; if (value === '') this.files = []; }
  get value() { return this._value || ''; }
}
function ui(respond = async () => response(capPayload)) {
  const nodes = new Map(), calls = [], events = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  $('pdfUploadCard').style.display = 'none';
  const context = { $, window: { FixEpubPdfConversion: pdf }, API: '', FormData: FormDataStub,
    document: { createElement: () => new Node(), createTextNode: text => ({ textContent: text }) },
    fetch: async (url, options) => { calls.push({ url, options }); return context.respond(url, options); }, respond,
    clientHeaders: () => ({ client: 'id' }), authHeaders: () => ({ token: 'original' }),
    localStorage: { getItem: () => null }, sessionStorage: { getItem: () => null },
    currentJobId: 'job-a', currentBatchId: null, currentJobStatus: 'awaiting_confirmation',
    saveJobToken: (id, token) => events.push(['token', id, token]),
    returnLater: { remember: task => events.push(['remember', task.id]) }, loadTaskList: () => events.push(['list']),
    openJobDetail: id => events.push(['open', id]),
    renderContinuePayment: (data, kind, id) => events.push(['payment', kind, id, data.status]),
    resumePaymentPolling: (kind, id) => events.push(['poll', kind, id]),
    continuePendingPayment: async () => events.push(['continue']), pollJobV2: id => events.push(['reload', id]),
  };
  vm.createContext(context);
  const source = html.slice(html.indexOf('    // PDF is deliberately independent'), html.indexOf('    const TRANSLATION_STRATEGY_LABELS'));
  vm.runInContext(source, context);
  return { context, $, calls, events, source };
}
function accept(h) {
  h.$('pdfWarnings').querySelectorAll('input[type=checkbox]').forEach(n => { n.checked = true; });
  h.$('pdfPreserveAcknowledgement').checked = true; h.context.refreshPdfConfirmation();
}

test('Actual UI starts hidden; only valid capability shows independent PDF chooser and server price', async () => {
  const h = ui(); assert.equal(h.$('pdfUploadCard').style.display, 'none'); await tick();
  assert.equal(h.$('pdfUploadCard').style.display, 'block'); assert.equal(h.$('pdfFileInput').accept, '.pdf');
  assert(h.$('pdfCapabilityHint').textContent.includes('¥0.99'));
  const off = ui(async () => response({})); await tick(); assert.equal(off.$('pdfUploadCard').style.display, 'none');
  assert(!off.$('pdfFileInput').accept);
});

test('Actual refresh renders dedicated safe confirmation and requires every warning plus range acknowledgement', async () => {
  const h = ui(); await tick(); h.context.renderPdfConversion(prepared(), 'job-a');
  assert.equal(h.$('pdfConfirmationPanel').style.display, 'block');
  assert(h.$('pdfPlanSummary').textContent.includes('346'));
  assert.equal(h.$('pdfWarnings').querySelectorAll('input[type=checkbox]').length, 3);
  assert(h.$('pdfConfirmBtn').disabled);
  h.$('pdfPreserveAcknowledgement').checked = true; h.context.refreshPdfConfirmation(); assert(h.$('pdfConfirmBtn').disabled);
  accept(h); assert.equal(h.$('pdfConfirmBtn').disabled, false);
  assert(!h.source.includes('innerHTML'));
});

test('Repeated same-plan refresh preserves checks; changed plan or job requires fresh acknowledgement', async () => {
  const h = ui(); await tick(); h.context.renderPdfConversion(prepared(), 'job-a'); accept(h);
  h.context.renderPdfConversion(prepared(), 'job-a'); assert.equal(h.$('pdfConfirmBtn').disabled, false);
  h.context.renderPdfConversion(prepared({ plan_id: 'b'.repeat(64) }), 'job-a'); assert(h.$('pdfConfirmBtn').disabled);
  accept(h); h.context.renderPdfConversion(prepared({ plan_id: 'b'.repeat(64) }, 'job-b'), 'job-b'); assert(h.$('pdfConfirmBtn').disabled);
});

test('Blocked/unknown-risk or completed plans never expose an actionable confirmation', async () => {
  const h = ui(); await tick();
  for (const data of [prepared({ can_confirm: false }), prepared({ warnings: ['unknown_new_warning'] })]) {
    h.context.renderPdfConversion(data, 'job-a'); accept(h); assert(h.$('pdfConfirmBtn').disabled);
  }
  h.context.renderPdfConversion({ ...prepared({ phase: 'confirmed', confirmed: true }), status: 'completed' }, 'job-a');
  assert.equal(h.$('pdfConfirmationPanel').style.display, 'none');
});

test('Actual confirmation reuses original payment controller/polling without direct popup or generic order creation', async () => {
  const h = ui(async url => response(url.endsWith('/capabilities') ? capPayload : { job_id: 'job-a', status: 'pending_payment' }));
  await tick(); h.context.renderPdfConversion(prepared(), 'job-a'); accept(h);
  await h.context.confirmPdfConversion();
  assert.deepStrictEqual(h.events.filter(e => ['payment', 'poll', 'continue'].includes(e[0])), [['payment', 'job', 'job-a', 'pending_payment'], ['poll', 'job', 'job-a'], ['continue']]);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 1);
  assert(!h.source.includes('window.open'));
});

test('Late confirmation cannot overwrite another task or open its payment', async () => {
  const gate = deferred(), h = ui(async url => url.endsWith('/capabilities') ? response(capPayload) : gate.promise);
  await tick(); h.context.renderPdfConversion(prepared(), 'job-a'); accept(h);
  const pending = h.context.confirmPdfConversion(); h.context.resetPdfConfirmation(); h.context.currentJobId = 'job-b';
  gate.resolve(response({ job_id: 'job-a', status: 'pending_payment' })); await pending;
  assert(!h.events.some(e => ['payment', 'continue'].includes(e[0])));
});

test('Late upload preserves its token/task record but never steals the newly opened task', async () => {
  const gate = deferred(), h = ui(async url => url.endsWith('/capabilities') ? response(capPayload) : gate.promise);
  await tick(); h.$('pdfFileInput').files = [file];
  const pending = h.context.uploadPdf(); h.context.resetPdfConfirmation(); h.context.currentJobId = 'job-b';
  gate.resolve(response({ job_id: 'job-new', access_token: 'new-token', status: 'queued' })); await pending;
  assert(h.events.some(e => e[0] === 'token' && e[1] === 'job-new'));
  assert(h.events.some(e => e[0] === 'remember' && e[1] === 'job-new'));
  assert(!h.events.some(e => e[0] === 'open'));
});

test('Warning-free plans still need explicit preserve-original acknowledgement; capability off does not hide an existing plan', async () => {
  const h = ui(async () => response({})); await tick();
  h.context.renderPdfConversion(prepared({ warnings: [] }), 'job-a');
  assert(h.$('pdfConfirmBtn').disabled);
  h.$('pdfPreserveAcknowledgement').checked = true; h.context.refreshPdfConfirmation();
  assert.equal(h.$('pdfConfirmBtn').disabled, false);
});

test('A stale confirmation refreshes the saved task and never initiates payment', async () => {
  const h = ui(async url => url.endsWith('/capabilities') ? response(capPayload) : response({ detail: '/private/book' }, 409));
  await tick(); h.context.renderPdfConversion(prepared(), 'job-a'); accept(h);
  await h.context.confirmPdfConversion();
  assert(h.events.some(e => e[0] === 'reload' && e[1] === 'job-a'));
  assert(!h.events.some(e => e[0] === 'continue'));
  assert.equal(h.$('pdfConfirmError').style.display, 'block');
  assert(!h.$('pdfConfirmError').textContent.includes('/private'));
});

test('Explicit server test queued response only resumes status and never opens payment', async () => {
  const h = ui(async url => response(url.endsWith('/capabilities') ? capPayload : { job_id: 'job-a', status: 'queued' }));
  await tick(); h.context.renderPdfConversion(prepared(), 'job-a'); accept(h);
  await h.context.confirmPdfConversion();
  assert(h.events.some(e => e[0] === 'poll'));
  assert(!h.events.some(e => e[0] === 'continue'));
});

test('Fresh upload stores the token before opening the task and keeps PDF fields separate', async () => {
  const h = ui(async url => response(url.endsWith('/capabilities') ? capPayload : { job_id: 'new', access_token: 'token', status: 'queued' }));
  await tick(); h.$('pdfFileInput').files = [file]; await h.context.uploadPdf();
  assert(h.events.findIndex(e => e[0] === 'token') < h.events.findIndex(e => e[0] === 'open'));
  assert.deepStrictEqual(h.calls.find(c => c.options.method === 'POST').options.body.fields.map(e => e[0]), ['file']);
  assert.equal(h.$('pdfFileInput').files.length, 0);
});

test('Homepage wires task recovery to PDF confirmation while generic forms still omit PDF', () => {
  assert(html.includes('pdf-conversion.js?v='));
  assert(html.includes('s === "awaiting_confirmation" && data.pdf_conversion'));
  assert(html.includes('renderPdfConversion(data, jobId)'));
  assert(html.includes('mapV2StatusText(j.status, j.enable_translation, !!j.pdf_conversion || j.output_mode === "original")'));
  const ordinary = html.match(/<input id="fileInput"[^>]*>/)[0];
  const folder = html.match(/<input id="folderInput"[^>]*>/)[0];
  assert(!ordinary.includes('.pdf') && !folder.includes('.pdf'));
  const dedicated = html.match(/<input id="pdfFileInput"[^>]*>/)[0];
  assert(!dedicated.includes('multiple') && !dedicated.includes('directory'));
  assert(html.includes('不翻译、不 OCR'));
});
