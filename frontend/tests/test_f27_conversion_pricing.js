const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const traditional = fs.readFileSync(path.join(__dirname, '..', 'traditional-to-simplified.html'), 'utf8');
const horizontal = fs.readFileSync(path.join(__dirname, '..', 'vertical-to-horizontal.html'), 'utf8');
const translator = fs.readFileSync(path.join(__dirname, '..', 'epub-translator.html'), 'utf8');

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

test('繁简和竖转横静态介绍保留新价与 AI 另计并进入统一主页', () => {
  assert(traditional.includes('繁简基础转换价为 <strong>¥0.99 / 本</strong>'));
  assert(traditional.includes('AI 精校不包含在基础价中'));
  assert(horizontal.includes('竖排改横排基础转换价为 <strong>¥0.99 / 本</strong>'));
  assert(horizontal.includes('AI 精校不包含在基础价中'));
  assert(traditional.includes('href="./?tool=simplified" data-entry-link="upload"'));
  assert(horizontal.includes('href="./?tool=horizontal" data-entry-link="upload"'));
  for (const page of [traditional, horizontal, translator]) {
    assert(!page.includes('$1.99'));
    assert(!page.includes('paypal'));
    assert(!page.includes('"price": "0"'));
  }
});

test('翻译介绍不复制固定收费逻辑，报价与操作由主页提供', () => {
  assert(translator.includes('AI 翻译按主页当前报价收费'));
  assert(translator.includes('href="./?tool=translate" data-entry-link="upload"'));
  assert(translator.includes('href="./?view=tasks" data-entry-link="tasks"'));
});
