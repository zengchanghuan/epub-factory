const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'about.html'), 'utf8');
const footer = html.match(/<footer class="site-footer">([\s\S]*?)<\/footer>/)[1];
const declaration = footer.match(/<section class="ai-declaration"[^>]*>([\s\S]*?)<\/section>/)[1];

test('AI 声明仅显示一次，位于页脚链接之后、页面最底部', () => {
  assert.strictEqual((html.match(/<section class="ai-declaration"/g) || []).length, 1);
  assert(footer.indexOf('class="ai-declaration"') > footer.indexOf('class="footer-links"'));
  assert(/<\/section>\s*$/.test(footer));
  assert(/<\/footer>\s*<\/body>\s*<\/html>\s*$/.test(html));
  assert(!html.includes('<div class="ai-declaration">'));
});

test('默认仅展示简短说明，完整声明使用原生可访问折叠控件', () => {
  const summary = declaration.match(/<p class="ai-declaration-summary">([^<]+)<\/p>/)[1];
  assert(summary.length < 65);
  assert(summary.includes('DeepSeek API'));
  assert(/<details>\s*<summary>查看完整 AI 技术与算法声明<\/summary>/.test(declaration));
  assert(!/<details[^>]*\bopen\b/.test(declaration));
  assert(html.includes('aria-label="AI 技术与算法声明"'));
  assert(html.includes('.ai-declaration summary:focus-visible'));
});

test('展开内容保留供应商、备案编号及完整服务协议入口', () => {
  assert(declaration.includes('北京深度求索人工智能基础技术研究有限公司（DeepSeek）'));
  assert(declaration.includes('网信算备110108970550101240011号'));
  assert(declaration.includes('Beijing-DeepseekChat-202404280016'));
  assert(declaration.includes('https://platform.deepseek.com'));
  assert(declaration.includes('https://cdn.deepseek.com/policies/zh-CN/deepseek-open-platform-terms-of-service.html'));
  const links = [...declaration.matchAll(/<a\s+([^>]+)>/g)];
  assert.strictEqual(links.length, 2);
  assert(links.every(link => /rel="noopener"/.test(link[1])));
});

test('页底说明无醒目蓝色卡片，长备案号可在手机端换行', () => {
  const styles = html.match(/\.ai-declaration \{([\s\S]*?)\}/)[1];
  assert(!styles.includes('background:'));
  assert(!styles.includes('border-radius:'));
  assert(styles.includes('font-size: 12px'));
  assert(/\.ai-declaration-content\s*\{[^}]*overflow-wrap:\s*anywhere/.test(html));
});
