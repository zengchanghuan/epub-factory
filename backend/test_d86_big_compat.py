"""EPUB 2 big migration preserves font hints, text, anchors and foreign trees."""
import unittest
from lxml import etree
from app.engine.html_compat import upgrade_legacy_html


class BigCompatibilityTests(unittest.TestCase):
    def root(self, body, head=''):
        return etree.fromstring(('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title>'+head+'</head><body>'+body+'</body></html>').encode())

    def by_id(self, root, value):
        return root.xpath('//*[@id=$value]',value=value)[0]

    def test_default_nested_scaling_text_links_and_ids(self):
        root=self.root('<p>Before <big id="a" class="keep">large <big id="b"><a href="#a">nested</a></big> tail</big> after</p>')
        text=''.join(root.itertext());upgrade_legacy_html(root)
        self.assertEqual(''.join(root.itertext()),text)
        for key in ['a','b']:
            node=self.by_id(root,key);self.assertEqual(etree.QName(node).localname,'span');self.assertEqual(node.get('style'),'font-size:larger')
        self.assertEqual(self.by_id(root,'a').get('class'),'keep')
        self.assertEqual(root.xpath('//*[local-name()="a"]/@href'),['#a'])
        after=etree.tostring(root);upgrade_legacy_html(root);self.assertEqual(etree.tostring(root),after)

    def test_prince_class_stylesheet_wins_without_inline_override(self):
        root=self.root('<big id="a" class="calibre5"><span>Title</span></big>', '<link rel="stylesheet" href="book.css"/>')
        calls=[]
        def load(href):calls.append(href);return b'.calibre5 {font-size: 1.125em} .other {color:red}'
        upgrade_legacy_html(root,stylesheet_loader=load)
        self.assertEqual(calls,['book.css']);node=self.by_id(root,'a')
        self.assertEqual(etree.QName(node).localname,'span');self.assertIsNone(node.get('style'))

    def test_inline_font_size_and_shorthand_and_css_wide_values(self):
        for style in ['font-size:12px!important','font:italic 14px serif','font-size:inherit','font:initial','all:unset']:
            with self.subTest(style=style):
                root=self.root(f'<big id="a" style="{style}">Text</big>');upgrade_legacy_html(root)
                self.assertEqual(etree.QName(self.by_id(root,'a')).localname,'span')
                self.assertEqual(self.by_id(root,'a').get('style'),style)

    def test_author_font_on_parent_does_not_remove_child_relative_scaling(self):
        root=self.root('<big id="a" class="sized">Large <big id="b">Larger</big></big>','<style>.sized {font-size:20px}</style>')
        upgrade_legacy_html(root)
        self.assertIsNone(self.by_id(root,'a').get('style'))
        self.assertEqual(self.by_id(root,'b').get('style'),'font-size:larger')

    def test_unrelated_style_and_inline_color_survive(self):
        root=self.root('<big id="a" style="color:red">Text</big>','<style>p {font-size:12px} .other {font-size:10px}</style>')
        upgrade_legacy_html(root)
        self.assertEqual(self.by_id(root,'a').get('style'),'color:red;font-size:larger')

    def test_unknown_or_type_sensitive_styles_do_not_change_big(self):
        styles=['big {color:red}','span {color:red}','p:first-of-type {color:red}',
                '.a > .b {color:red}','@media screen {.x {font-size:12px}}',
                '@supports (display:grid) {.x {font-size:12px}}','@import "remote.css";',
                '.x {font-size:var(--size)}','.x {font-size:revert}',
                '.x {font-size:garbage}','[style] {color:red}', '.x {all:revert-layer}']
        for css in styles:
            with self.subTest(css=css):
                root=self.root('<big id="a" class="x">Text</big>',f'<style>{css}</style>');before=etree.tostring(root)
                upgrade_legacy_html(root);self.assertEqual(etree.tostring(root),before)

    def test_missing_conditional_alternate_stylesheets_are_not_guessed(self):
        for attrs in ['',' media="print"',' title="Alternative"',' disabled="disabled"']:
            root=self.root('<big>Text</big>',f'<link rel="stylesheet" href="book.css"{attrs}/>')
            before=etree.tostring(root)
            upgrade_legacy_html(root,stylesheet_loader=lambda href:None if not attrs else b'.x {color:red}')
            self.assertEqual(etree.tostring(root),before)

    def test_foreign_math_svg_and_xhtml_inside_svg_unchanged(self):
        root=self.root('<svg xmlns="http://www.w3.org/2000/svg"><big>SVG</big><foreignObject><big xmlns="http://www.w3.org/1999/xhtml">Foreign</big></foreignObject></svg><math xmlns="http://www.w3.org/1998/Math/MathML"><big>Math</big></math><x:big xmlns:x="urn:custom">Other</x:big>')
        before=etree.tostring(root);upgrade_legacy_html(root);self.assertEqual(etree.tostring(root),before)

    def test_no_css_loading_without_big(self):
        root=self.root('<span>Ordinary</span>','<link rel="stylesheet" href="book.css"/>')
        def forbidden(_):raise AssertionError('unnecessary CSS load')
        upgrade_legacy_html(root,stylesheet_loader=forbidden)

    def test_nested_uncertain_font_is_atomic(self):
        root=self.root('<big>Outer<big style="font-size:var(--size)">Inner</big></big>')
        before=etree.tostring(root);upgrade_legacy_html(root);self.assertEqual(etree.tostring(root),before)

    def test_non_css_link_is_not_loaded(self):
        root=self.root('<big id="a">Text</big>','<link rel="stylesheet" type="application/vnd.adobe-page-template+xml" href="page.xpgt"/>')
        def forbidden(_):raise AssertionError('non-CSS must not be loaded')
        upgrade_legacy_html(root,stylesheet_loader=forbidden)
        self.assertEqual(etree.QName(self.by_id(root,'a')).localname,'span')

    def test_invalid_stylesheet_and_revert_inline_stay_closed(self):
        for head,style in [('<style>.x { font-size: 12px; broken</style>',''),('', 'font-size:revert'),('', 'font:var(--font)')]:
            root=self.root(f'<big class="x" style="{style}">Text</big>',head)
            before=etree.tostring(root);upgrade_legacy_html(root);self.assertEqual(etree.tostring(root),before)


if __name__=='__main__':unittest.main()
