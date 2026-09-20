const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

test('首页展示标准翻译新最低价和阶梯价格', () => {
  assert(html.includes('标准 AI 翻译 ¥3.99 起'));
  assert(html.includes('前 30 万字符按 ¥0.05 / 1,000 字符计费'));
  assert(html.includes('长书超出部分自动采用更低阶梯单价'));
});

test('首页不再宣传旧的统一单价和最低价', () => {
  assert(!html.includes('¥0.10 / 1,000 字符'));
  assert(!html.includes('AI 翻译按实际字符数计费（¥0.10'));
  assert(!html.includes('最低 ¥5.99'));
});

test('高质量、文学和 Pro 明确按所选档位报价', () => {
  assert(html.includes('高质量、文学和 Pro 模式按所选档位报价'));
});
