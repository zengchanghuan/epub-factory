const fs = require('fs');
const path = require('path');
const vm = require('vm');
const entry = require('../tool-entry.js');
const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');

function harness(query = '', inputs = []) {
  const location = new URL('https://example.test/' + query);
  const listeners = new Map();
  const form = {
    querySelectorAll: () => inputs,
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: name => listeners.delete(name),
  };
  const history = { replaceState(_, __, target) { location.href = new URL(target, location).href; } };
  const calls = [];
  const controller = entry.capture({ location, history, form });
  const state = { setMode: mode => calls.push(['mode', mode]), setConversion: value => calls.push(['conversion', value]), showTasks: () => calls.push(['tasks']) };
  return { location, listeners, form, controller, state, calls };
}

for (const [tool, expected] of [
  ['translate', [['mode', 'translate']]],
  ['horizontal', [['mode', 'convert']]],
  ['simplified', [['conversion', 'simplified|auto'], ['mode', 'convert']]],
]) {
  test(`R11 preset ${tool}: applied exactly once, URL consumed`, () => {
    const h = harness('?tool=' + tool + '&keep=1#anchor');
    assert(h.controller.apply(h.state));
    assert.deepStrictEqual(h.calls, expected);
    assert.equal(h.location.search, '?keep=1');
    assert.equal(h.location.hash, '#anchor');
    assert.equal(h.controller.apply(h.state), false);
    assert.equal(h.listeners.size, 0);
  });
}

test('R11 unknown, duplicate and hostile tool values do not change the form', () => {
  for (const query of ['?tool=translate&tool=simplified', '?tool=//evil.test', '?tool=Translate', '?tool=', '?tool=tasks']) {
    const h = harness(query);
    assert.equal(h.controller.apply(h.state), false);
    assert.deepStrictEqual(h.calls, []);
    assert(!h.location.searchParams.has('tool'));
  }
});

test('R11 job/batch URL always outranks mode, including empty/invalid identifiers', () => {
  for (const param of ['job_id=book', 'batch_id=batch', 'job_id=', 'batch_id=../bad']) {
    const h = harness('?tool=translate&' + param + '#access_token=secret');
    assert.equal(h.controller.apply(h.state), false);
    assert(h.location.hash.includes('access_token=secret'));
    assert.deepStrictEqual(h.calls, []);
  }
});

test('R11 active task or selected files outrank entry presets', () => {
  for (const state of [{ hasActiveTask: true }, { hasFiles: true }]) {
    const h = harness('?tool=simplified');
    assert.equal(h.controller.apply({ ...h.state, ...state }), false);
    assert.deepStrictEqual(h.calls, []);
  }
});

test('R11 restored text/radio/select/file values and later edits outrank preset', () => {
  for (const input of [
    { type: 'text', value: 'existing', defaultValue: '' },
    { type: 'radio', checked: true, defaultChecked: false },
    { type: 'file', files: [{}] },
    { tagName: 'SELECT', options: [{ selected: false, defaultSelected: true }, { selected: true, defaultSelected: false }] },
  ]) {
    const h = harness('?tool=translate', [input]);
    assert.equal(h.controller.apply(h.state), false);
  }
  const h = harness('?tool=simplified');
  h.listeners.get('input')();
  assert.equal(h.controller.apply(h.state), false);
});

test('R11 untouched controls, including implicit first option, are pristine', () => {
  assert.equal(entry.hasRestoredInputs({ querySelectorAll: () => [
    { type: 'checkbox', checked: false, defaultChecked: false },
    { type: 'text', value: '', defaultValue: '' },
    { type: 'file', files: [] },
    { tagName: 'SELECT', options: [{ selected: true, defaultSelected: false }, { selected: false, defaultSelected: false }] },
  ] }), false);
});

test('R11 task-center entry only navigates the view; it does not choose a mode', () => {
  const h = harness('?view=tasks&tool=translate');
  h.controller.apply(h.state);
  assert.deepStrictEqual(h.calls, [['tasks']]);
  assert.equal(h.location.search, '');
});

test('R11 legacy handoff stays on the same host/path, forwards IDs and fragment, strips arbitrary query', () => {
  const location = new URL('https://example.test/app/epub-translator.html?job_id=book&batch_id=batch&admin_key=local&redirect=https://evil.test&token=never#access_token=abc%2Bdef');
  const target = new URL(entry.buildTarget('translate', location));
  assert.equal(target.origin, location.origin);
  assert.equal(target.pathname, '/app/');
  assert.equal(target.search, '?job_id=book&batch_id=batch&admin_key=local');
  assert.equal(target.hash, location.hash);
  assert(!target.search.includes('access_token'));
});

test('R11 direct file preview points to index.html, never a directory', () => {
  assert.equal(entry.buildTarget('horizontal', new URL('file:///tmp/frontend/vertical-to-horizontal.html')), 'file:///tmp/frontend/index.html?tool=horizontal');
});

test('R11 no-task OAuth fragment survives normal navigation', () => {
  const target = new URL(entry.buildTarget('translate', new URL('https://example.test/epub-translator.html#access_token=oauth&state=x')));
  assert.equal(target.hash, '#access_token=oauth&state=x');
  assert.equal(target.search, '?tool=translate');
});

test('R11 landing mount updates native CTAs and only auto-forwards existing tasks', () => {
  for (const query of ['', '?job_id=book#access_token=secret']) {
    const links = [{ dataset: { entryLink: 'upload' } }, { dataset: { entryLink: 'tasks' } }];
    const location = new URL('https://example.test/epub-translator.html' + query);
    const forwarded = [];
    location.replace = target => forwarded.push(target);
    entry.mountLanding({ location, document: { body: { dataset: { toolEntry: 'translate' } }, querySelectorAll: () => links } });
    assert.equal(forwarded.length, query ? 1 : 0);
    assert.equal(new URL(links[1].href).search, query ? '?job_id=book' : '?view=tasks');
  }
});

test('R11 restricted browser history does not abort preset or recovery initialization', () => {
  const controller = entry.capture({ location: new URL('https://example.test/?tool=translate'), history: { replaceState() { throw new Error('denied'); } } });
  let mode;
  assert(controller.apply({ setMode(value) { mode = value; } }));
  assert.equal(mode, 'translate');
});

test('R11 actual homepage bootstrap orders token import/recovery before final one-shot preset', () => {
  const captureAt = html.indexOf('const toolEntry =');
  const importAt = html.indexOf('FixEpubReturnLater.importFragment(');
  const recoverAt = html.indexOf('(function initFromQuery()');
  const setupAt = html.indexOf('    syncConversionMode();\n    refreshTaskModeAvailability();');
  const endAt = html.indexOf('    function setStatus', setupAt);
  assert(captureAt < importAt && importAt < recoverAt && recoverAt < setupAt);
  assert.equal((html.match(/toolEntry\.apply\(/g) || []).length, 1);
  const setup = html.slice(setupAt, endAt);
  for (const query of ['?tool=translate', '?tool=translate&job_id=old']) {
    const h = harness(query), calls = [];
    const context = {
      syncConversionMode() {}, refreshTaskModeAvailability() {}, getTaskMode: () => 'convert',
      setTaskMode: mode => calls.push(mode), toolEntry: h.controller,
      currentJobId: query.includes('job_id') ? 'old' : null, currentBatchId: null,
      selectedFiles: [], showView() { throw new Error('Unexpected navigation'); },
    };
    vm.runInNewContext(setup, context);
    assert.deepStrictEqual(calls, query.includes('job_id') ? ['convert'] : ['convert', 'translate']);
  }
});

test('R11 homepage preserves restored mode and degrades to the normal UI if adapter fails to load', () => {
  const setupAt = html.indexOf('    syncConversionMode();\n    refreshTaskModeAvailability();');
  const setup = html.slice(setupAt, html.indexOf('    function setStatus', setupAt));
  const modes = [];
  vm.runInNewContext(setup, { syncConversionMode() {}, refreshTaskModeAvailability() {}, getTaskMode: () => 'translate', setTaskMode: mode => modes.push(mode), toolEntry: undefined });
  assert.deepStrictEqual(modes, ['translate']);
  assert(html.includes('window.FixEpubToolEntry && window.FixEpubToolEntry.capture'));
});
