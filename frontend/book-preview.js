/* Read-only chapter controller. The server sanitizes HTML; iframe stays sandboxed. */
class BookPreviewController {
  constructor({ fetchChapter, render, state, error }) {
    Object.assign(this, { fetchChapter, render, state, error });
    this.generation = 0; this.jobId = ''; this.index = 0; this.total = 0;
  }
  open(jobId) {
    this.close(); this.jobId = jobId; this.index = 0; this.total = 0;
    return this.load(0);
  }
  async load(index) {
    if (!this.jobId || index < 0 || (this.total && index >= this.total)) return false;
    if (this.abort) this.abort.abort();
    this.abort = new AbortController();
    const generation = ++this.generation;
    this.state(true);
    try {
      const data = await this.fetchChapter(this.jobId, index, this.abort.signal);
      if (generation !== this.generation) return false;
      if (!data || typeof data.html !== 'string' || !Array.isArray(data.chapters)) throw new Error('预览数据不完整');
      this.index = data.chapter_index; this.total = data.total_chapters;
      this.render(data); return true;
    } catch (error) {
      if (generation === this.generation && error.name !== 'AbortError') this.error(error.message || '预览失败');
      return false;
    } finally {
      if (generation === this.generation) this.state(false);
    }
  }
  go(offset) { return this.load(this.index + offset); }
  close() {
    ++this.generation;
    if (this.abort) this.abort.abort();
    this.jobId = ''; this.total = 0; this.render(null); this.state(false);
  }
}
function previewKeyAction(key, tagName) {
  if (key === 'Escape') return 'close';
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(String(tagName).toUpperCase())) return '';
  return key === 'ArrowLeft' ? 'previous' : key === 'ArrowRight' ? 'next' : '';
}
function feedbackStillCurrent(reader, jobId, generation) {
  return reader.jobId === jobId && reader.generation === generation;
}
if (typeof module !== 'undefined' && module.exports) module.exports = { BookPreviewController, previewKeyAction, feedbackStillCurrent };
if (typeof window !== 'undefined') Object.assign(window, { BookPreviewController, previewKeyAction, feedbackStillCurrent });
