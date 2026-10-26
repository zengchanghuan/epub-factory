"""Render published synthetic outputs, never invented full-book evidence."""
import json
from html import escape
from pathlib import Path

LABELS = {
 'zh-CN': ('双语阅读效果示意', '下列短文来自已公开的合成文本模型结果。目录与排版仅为示意，不代表整本书质量。', '第 1 章 · 对话', '原文', '译文'),
 'zh-TW': ('雙語閱讀效果示意', '下列短文來自已公開的合成文字模型結果。目錄與排版僅為示意，不代表整本書品質。', '第 1 章 · 對話', '原文', '譯文'),
 'en': ('Bilingual reading preview', 'Published model output on a synthetic passage. Contents and layout are illustrative, not whole-book quality evidence.', 'Chapter 1 · A conversation', 'Original', 'Translation'),
 'ja': ('対訳レイアウトの例', '公開済みの合成短文モデル出力です。目次とレイアウトは模式例で、書籍全体の品質を保証しません。', '第1章 · 会話', '原文', '訳文'),
 'ko': ('원문 대조 읽기 예시', '공개된 합성 단문 모델 결과입니다. 목차와 레이아웃은 예시이며 책 전체 품질을 나타내지 않습니다.', '1장 · 대화', '원문', '번역'),
}

def render_sample(language):
    sample = json.loads((Path(__file__).resolve().parents[1] / 'frontend/examples-data.json').read_text())[language]['translation']
    title, note, chapter, before, after = LABELS[language]
    return f'''<section class="panel" id="translation-sample"><h2>{escape(title)}</h2><p>{escape(note)}</p>
<nav><a href="#sample-chapter">{escape(chapter)}</a></nav><h3 id="sample-chapter">{escape(chapter)}</h3>
<p><strong>{escape(before)}</strong></p><blockquote lang="{sample['source_language']}">{escape(sample['before'])}</blockquote>
<p><strong>{escape(after)}</strong></p><blockquote lang="{language}">{escape(sample['after'])}</blockquote></section>'''
