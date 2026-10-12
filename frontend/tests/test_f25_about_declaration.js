const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'about.html'), 'utf8');
const main = html.match(/<main\b[^>]*>([\s\S]*?)<\/main>/)[1];
const footer = html.match(/<footer class="site-footer">([\s\S]*?)<\/footer>/)[1];

test('服务介绍说明自动处理方式，并在正文提供隐私政策入口', () => {
  assert(/自动翻译/.test(main));
  assert(/文本[\s\S]*第三方[\s\S]*处理/.test(main));
  const privacyLink = main.match(/<a\s+href="([^"]+)"[^>]*>隐私政策<\/a>/);
  assert(privacyLink, '处理说明必须提供隐私政策链接');
  assert.strictEqual(privacyLink[1], '/privacy.html');
  assert(fs.existsSync(path.join(__dirname, '..', privacyLink[1])));
});

test('公司身份、客服入口和网站政策链接继续可用', () => {
  assert(footer.includes('深圳市长欢文化艺术有限公司'));
  assert(/<a\s+href="mailto:[^"]+"/.test(footer));
  assert(/<button[^>]+data-copy-qq/.test(footer));
  assert(/role="status"\s+aria-live="polite"/.test(footer));
  for (const destination of ['privacy', 'terms', 'refund', 'contact']) {
    assert(footer.includes(`href="/${destination}.html"`));
    assert(fs.existsSync(path.join(__dirname, '..', `${destination}.html`)));
  }
});
