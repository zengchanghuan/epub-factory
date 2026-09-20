const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const traditional = fs.readFileSync(path.join(__dirname, '..', 'traditional-to-simplified.html'), 'utf8');
const horizontal = fs.readFileSync(path.join(__dirname, '..', 'vertical-to-horizontal.html'), 'utf8');

test('繁简转换和竖排改横排展示 0.99 元基础价', () => {
  assert(html.includes('基础转换：<strong style="color:var(--text);">¥0.99 / 本</strong>'));
  assert(html.includes('¥0.99 × ${selectedFiles.length}'));
  assert(html.includes('data.amount || "0.99"'));
});

test('AI 精校明确作为附加费单独列示', () => {
  assert(html.includes('AI 精校另计'));
  assert(html.includes('data.precision_polish_amount'));
  assert(html.includes('基础转换 ¥${baseAmount} + AI 精校 ¥${polishAmount.toFixed(2)}'));
  assert(html.includes('if (!isTranslationMode)'));
});

test('首页不再展示 1.99 元的基础转换价', () => {
  assert(!html.includes('格式转换服务：<strong style="color:var(--text);">¥1.99 / 次</strong>'));
  assert(!html.includes('¥1.99 × ${selectedFiles.length}'));
});

test('繁简和竖转横独立入口同步新价与 AI 另计说明', () => {
  assert(traditional.includes('繁简基础转换价为 <strong>¥0.99 / 本</strong>'));
  assert(traditional.includes('AI 精校不包含在基础价中'));
  assert(horizontal.includes('竖排改横排基础转换价为 <strong>¥0.99 / 本</strong>'));
});
