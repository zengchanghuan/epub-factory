const { BookPreviewController, previewKeyAction, feedbackStillCurrent } = require('../book-preview.js');
const fixture = index => ({ html: '<p>中文</p>', chapters: [{ index: 0, title: '第一章' }, { index: 1, title: '第二章' }], chapter_index: index, total_chapters: 2 });
function controller(fetchChapter) {
  const output = { render: [], states: [], errors: [] };
  const reader = new BookPreviewController({ fetchChapter, render: data => output.render.push(data), state: data => output.states.push(data), error: data => output.errors.push(data) });
  return { reader, output };
}
test('F22-1 请求有权限的当前书，切章不越界', async () => {
  const calls = [];
  const { reader } = controller(async (id, index, signal) => { calls.push({ id, index, signal }); return fixture(index); });
  await reader.open('book'); await reader.go(-1); await reader.go(1); await reader.go(1);
  assert.deepEqual(calls.map(c => [c.id, c.index]), [['book', 0], ['book', 1]]);
  assert.ok(calls.every(c => c.signal instanceof AbortSignal));
});
test('F22-2 关闭后慢响应不可重新填入书稿内容', async () => {
  let finish;
  const { reader, output } = controller(() => new Promise(resolve => { finish = resolve; }));
  const waiting = reader.open('book'); reader.close(); finish(fixture(0)); await waiting;
  assert.equal(output.render.at(-1), null); assert.equal(reader.jobId, '');
});
test('F22-3 快速切换书稿不会混入上一书的响应', async () => {
  const resolve = {};
  const { reader, output } = controller(id => new Promise(done => { resolve[id] = done; }));
  const first = reader.open('first'); const second = reader.open('second');
  resolve.second(fixture(1)); await second; resolve.first(fixture(0)); await first;
  assert.equal(reader.jobId, 'second'); assert.equal(output.render.at(-1).chapter_index, 1);
});
test('F22-4 出错显示提示并结束加载，不报告成功', async () => {
  const { reader, output } = controller(async () => { throw new Error('无权访问该任务'); });
  assert.equal(await reader.open('book'), false);
  assert.deepEqual(output.errors, ['无权访问该任务']); assert.equal(output.states.at(-1), false);
});
test('F22-5 输入反馈时箭头不误切章，Escape 可以关闭', () => {
  assert.equal(previewKeyAction('ArrowLeft', 'BUTTON'), 'previous');
  assert.equal(previewKeyAction('ArrowRight', 'TEXTAREA'), '');
  assert.equal(previewKeyAction('Escape', 'TEXTAREA'), 'close');
});
test('F22-6 同一本书关闭重开，旧反馈响应不可清空新反馈', async () => {
  const { reader } = controller(async () => fixture(0));
  await reader.open('same'); const generation = reader.generation;
  assert.equal(feedbackStillCurrent(reader, 'same', generation), true);
  reader.close(); await reader.open('same');
  assert.equal(feedbackStillCurrent(reader, 'same', generation), false);
});
