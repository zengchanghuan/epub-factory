"""Technical-book examples must survive typography/CJK preprocessing literally."""
import unittest
from bs4 import BeautifulSoup

from app.engine.cleaners.cjk_normalizer import CjkNormalizer
from app.engine.cleaners.typography_enhancer import TypographyEnhancer
from app.utils.html_text import map_html_text, map_html_styles


class LiteralCodePreservationTests(unittest.TestCase):
    def run_cleaners(self, markup, mode="simplified"):
        content = markup.encode()
        for cleaner in (CjkNormalizer(output_mode=mode), TypographyEnhancer()):
            content = cleaner.process(content, 9)
        return content.decode()

    def test_python_and_shell_punctuation_remain_executable(self):
        code = 'python --version\nvalue = ...\nprint("...")\n----\n'
        markup = '<html><body><pre>' + code + '</pre><p>Wait... Note -- see below.</p></body></html>'
        result = self.run_cleaners(markup, "keep")
        self.assertEqual(BeautifulSoup(result, "html.parser").pre.get_text(), code)
        self.assertIn('Wait… Note — see below.', result)

    def test_code_pre_kbd_samp_tt_protect_cjk_punctuation_and_nested_highlighting(self):
        for tag in ("pre", "code", "kbd", "samp", "tt"):
            for mode in ("simplified", "traditional", "keep"):
                with self.subTest(tag=tag, mode=mode):
                    literal = '<span class="syntax">軟體...</span><b>﹁滑鼠﹂ -- 文件</b>'
                    markup = f'<html><body><{tag} data-id="literal">{literal}</{tag}><p>軟體...﹁滑鼠﹂</p></body></html>'
                    result = self.run_cleaners(markup, mode)
                    self.assertIn(f'<{tag} data-id="literal">{literal}</{tag}>', result)
                    if mode == "simplified":
                        self.assertIn('<p>软件…「鼠标」</p>', result)

    def test_css_literals_remain_literal_but_real_styles_are_horizontalized(self):
        css = 'writing-mode: vertical-rl; direction: rtl;'
        markup = ('<html><head><style>' + css + '</style></head><body>'
                  '<p style="' + css + '">正文</p><pre><code>' + css + '</code></pre>'
                  '<p>Example: ' + css + '</p></body></html>')
        result = self.run_cleaners(markup, "keep")
        soup = BeautifulSoup(result, "html.parser")
        self.assertEqual(soup.pre.get_text(), css)
        self.assertEqual(soup.find_all('p')[-1].get_text(), 'Example: ' + css)
        self.assertIn('horizontal-tb', soup.style.get_text())
        self.assertIn('horizontal-tb', soup.p['style'])
        self.assertIn('direction: ltr;', soup.p['style'])

    def test_math_svg_scripts_comments_and_attributes_stay_unchanged(self):
        blocks = [
            '<math><mtext>軟體... -- ﹁﹂</mtext></math>',
            '<svg viewBox="0 0 20 20"><text>軟體... -- ﹁﹂</text></svg>',
            '<script>const 軟體 = "... --";</script>',
            '<!-- 軟體... -- -->',
        ]
        result = self.run_cleaners('<html><body>' + ''.join(blocks) +
                                   '<p title="軟體... --">文字...</p></body></html>')
        for block in blocks:
            self.assertIn(block, result)
        self.assertIn('title="軟體... --"', result)

    def test_raw_markup_and_nested_protected_boundaries_are_preserved(self):
        markup = '<PRE>literal<code>nested...</code> outer...</PRE><p>outside...</p>'
        self.assertEqual(map_html_text(markup, lambda s: s.replace('...', '…')),
                         '<PRE>literal<code>nested...</code> outer...</PRE><p>outside…</p>')
        css = '<STYLE><![CDATA[p {writing-mode: vertical-rl;}]]></STYLE>'
        self.assertEqual(map_html_styles(css, lambda s: s.replace('vertical-rl', 'horizontal-tb')),
                         '<STYLE><![CDATA[p {writing-mode: horizontal-tb;}]]></STYLE>')

    def test_typography_statistics_exclude_literal_code(self):
        cleaner = TypographyEnhancer()
        result = cleaner.process(b'<pre>... --</pre><p>... --</p>', 9)
        self.assertEqual(result, '<pre>... --</pre><p>… —</p>'.encode())
        self.assertEqual(cleaner.stats['typography_fixed'], 1)

    def test_css_mapper_only_changes_real_attributes_including_legacy_unquoted_style(self):
        markup = ('<html><body><p title="style=\'writing-mode:vertical-rl\'" '
                  'data-example="style=direction:rtl" style=writing-mode:vertical-rl>'
                  '正文</p></body></html>')
        result = self.run_cleaners(markup, 'keep')
        self.assertIn('title="style=\'writing-mode:vertical-rl\'"', result)
        self.assertIn('data-example="style=direction:rtl"', result)
        self.assertIn('style="writing-mode: horizontal-tb;"', result)


if __name__ == '__main__':
    unittest.main()
