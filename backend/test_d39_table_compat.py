"""B2 offline table migration: preserve content and author CSS precedence."""
import unittest
from lxml import etree
import cssutils

from app.engine.html_compat import upgrade_legacy_html


def tree(body, styles=''):
    return etree.fromstring(('<html xmlns="http://www.w3.org/1999/xhtml"><head>' + styles +
                             '</head><body>' + body + '</body></html>').encode())


def find(root, identifier):
    return root.xpath('//*[@id=$identifier]', identifier=identifier)[0]


def declarations(node):
    return {item.name: item.value for item in cssutils.parseStyle(node.get('style', '')).getProperties()}


class TableCompatibilityTests(unittest.TestCase):
    def test_legacy_zero_attributes_and_valign_become_legal_css(self):
        root = tree('<table id="t" border="0" cellspacing="0" cellpadding="0"><tr>'
                    '<td id="c" valign="top">Text<a id="a" href="#note">1</a></td>'
                    '<th id="h" valign="middle">Header</th></tr></table><p id="note">Note.</p>')
        text = ''.join(root.itertext())
        upgrade_legacy_html(root)
        table, cell = find(root, 't'), find(root, 'c')
        self.assertFalse({'border', 'cellspacing', 'cellpadding'} & set(table.attrib))
        self.assertNotIn('valign', cell.attrib)
        self.assertEqual(declarations(table)['border-spacing'], '0')
        for side in ('top', 'right', 'bottom', 'left'):
            self.assertEqual(declarations(table)[f'border-{side}-width'], '0')
            self.assertEqual(declarations(cell)[f'padding-{side}'], '0')
        self.assertEqual(declarations(cell)['vertical-align'], 'top')
        self.assertEqual(declarations(find(root, 'h'))['vertical-align'], 'middle')
        self.assertEqual(''.join(root.itertext()), text)
        self.assertEqual(find(root, 'a').get('href'), '#note')
        self.assertEqual(table.get('data-legacy-cellpadding'), '0')

    def test_existing_shorthands_are_never_overridden(self):
        root = tree('<table id="t" border="0" cellspacing="0" cellpadding="4" '
                    'style="border:3px double red;border-spacing:9px"><tr>'
                    '<td id="c" valign="top" style="padding:2px 5px!important;vertical-align:bottom">Body</td>'
                    '</tr></table>')
        originals = {id: find(root, id).get('style') for id in ('t', 'c')}
        upgrade_legacy_html(root)
        for id, style in originals.items():
            self.assertEqual(find(root, id).get('style'), style)
        self.assertNotIn('cellpadding', find(root, 't').attrib)

    def test_longhand_and_side_shorthand_only_protect_their_own_sides(self):
        root = tree('<table id="t" border="0" cellpadding="4" '
                    'style="border-left:3px dashed red;border-top-width:7px;border-right-color:blue"><tr>'
                    '<td id="c" style="padding-left:8px!important;padding-top:2px">Body</td></tr></table>')
        upgrade_legacy_html(root)
        table, cell = declarations(find(root, 't')), declarations(find(root, 'c'))
        self.assertEqual(table['border-left'], '3px dashed red')
        self.assertEqual(table['border-top-width'], '7px')
        self.assertNotIn('border-left-width', table)
        self.assertEqual(table['border-bottom-width'], '0')
        self.assertEqual(table['border-right-width'], '0')
        self.assertEqual(table['border-right-color'], 'blue')
        self.assertEqual(cell['padding-left'], '8px')
        self.assertEqual(cell['padding-top'], '2px')
        self.assertEqual(cell['padding-right'], '4px')
        self.assertEqual(cell['padding-bottom'], '4px')
        self.assertIn('padding-left:8px!important', find(root, 'c').get('style'))

    def test_global_all_and_border_width_shorthands_are_respected(self):
        root = tree('<table id="t" border="0" cellpadding="2" style="border-width:7px"><tr>'
                    '<td id="c" valign="top" style="all:initial">Body</td></tr></table>')
        upgrade_legacy_html(root)
        self.assertEqual(find(root, 't').get('style'), 'border-width:7px')
        self.assertEqual(find(root, 'c').get('style'), 'all:initial')
        self.assertNotIn('cellpadding', find(root, 't').attrib)
        self.assertNotIn('valign', find(root, 'c').attrib)

    def test_nested_table_cellpadding_never_leaks_into_nested_cells(self):
        root = tree('<table id="outer" cellpadding="8"><tr><td id="outercell">Outer'
                    '<table id="inner" cellpadding="2"><tr><td id="innercell">Inner</td></tr></table>'
                    '<table id="plain"><tr><td id="plaincell">Plain</td></tr></table>'
                    '</td></tr></table>')
        upgrade_legacy_html(root)
        self.assertEqual(declarations(find(root, 'outercell'))['padding-top'], '8px')
        self.assertEqual(declarations(find(root, 'innercell'))['padding-top'], '2px')
        self.assertNotIn('style', find(root, 'plaincell').attrib)

    def test_col_wrapping_keeps_nodes_order_ids_widths_text_and_tails(self):
        root = tree('<table id="t"><caption>Caption</caption><col id="a" width="20%"/>\n'
                    '<col id="b" width="80%"/>\n<colgroup id="original"><col id="c" width="12"/></colgroup>'
                    '<tr><td>Cell</td></tr></table>')
        a, b, c = (find(root, identifier) for identifier in ('a', 'b', 'c'))
        text = ''.join(root.itertext())
        tails = a.tail, b.tail, c.tail
        upgrade_legacy_html(root)
        table = find(root, 't')
        self.assertEqual([etree.QName(node).localname for node in table], ['caption', 'colgroup', 'colgroup', 'tr'])
        self.assertIs(table[1][0], a)
        self.assertIs(table[1][1], b)
        self.assertIs(table[2][0], c)
        self.assertEqual(table[2].get('id'), 'original')
        self.assertEqual((a.tail, b.tail, c.tail), tails)
        self.assertEqual(''.join(root.itertext()), text)
        self.assertEqual([declarations(node)['width'] for node in (a, b, c)], ['20%', '80%', '12px'])

    def test_wrapping_does_not_reorder_across_comments_or_existing_groups(self):
        root = tree('<table id="t"><col id="a"/><!--keep--><col id="b"/>'
                    '<colgroup id="old"><col id="c"/></colgroup><tr><td>Body</td></tr></table>')
        upgrade_legacy_html(root)
        table = find(root, 't')
        self.assertEqual(table[1].text, 'keep')
        self.assertEqual([node.get('id') for node in table.iter() if node.get('id')], ['t', 'a', 'b', 'old', 'c'])

    def test_author_stylesheet_precedence_matches_real_corpus(self):
        root = tree('<table id="t" class="calibre175" border="0" cellspacing="0" cellpadding="0">'
                    '<tr class="calibre177"><td id="c" class="calibre178" valign="top">Body</td></tr></table>',
                    '<link rel="stylesheet" href="../stylesheet.css"/>'
                    '<link rel="stylesheet" type="application/vnd.adobe-page-template+xml" href="page.xpgt"/>')
        css = ('.calibre175 {border-spacing:2px} .calibre177 {vertical-align:middle}'
               '.calibre178 {padding:1px;vertical-align:inherit}')
        upgrade_legacy_html(root, stylesheet_loader=lambda href: css if href == '../stylesheet.css' else None)
        self.assertNotIn('border-spacing', declarations(find(root, 't')))
        self.assertNotIn('style', find(root, 'c').attrib)
        self.assertNotIn('cellpadding', find(root, 't').attrib)
        self.assertNotIn('cellspacing', find(root, 't').attrib)
        self.assertNotIn('valign', find(root, 'c').attrib)

    def test_author_rules_match_only_the_intended_nodes_and_side(self):
        root = tree('<table id="t" cellpadding="3"><tr><td id="a" class="selected">A</td>'
                    '<td id="b">B</td></tr></table>', '<style>td.selected {padding-left:8px}'
                    'table {padding:10px} .other {padding:99px}</style>')
        upgrade_legacy_html(root)
        self.assertNotIn('padding-left', declarations(find(root, 'a')))
        self.assertEqual(declarations(find(root, 'a'))['padding-right'], '3px')
        self.assertEqual(declarations(find(root, 'b'))['padding-left'], '3px')

    def test_unknown_css_preserves_original_attribute_to_fail_delivery_closed(self):
        styles = ['<style>table > tr > td {padding:1px}</style>',
                  '<style>@media print {td {padding:1px}}</style>',
                  '<style>@supports (display:grid) {td {padding:9px}}</style>',
                  '<style>@layer author {td {padding:9px}}</style>',
                  '<style>@namespace "urn:other"; td {padding:9px}</style>',
                  '<style>@import "unknown.css";</style>',
                  '<style>td:hover {padding:1px}</style>',
                  '<link rel="stylesheet" href="unknown.css"/>']
        for sheet in styles:
            with self.subTest(sheet=sheet):
                root = tree('<table id="t" cellpadding="0"><tr><td id="c">Body</td></tr></table>', sheet)
                upgrade_legacy_html(root)
                self.assertEqual(find(root, 't').get('cellpadding'), '0')
                self.assertEqual(find(root, 't').get('data-legacy-cellpadding'), '0')
                self.assertNotIn('style', find(root, 'c').attrib)

    def test_invalid_author_declarations_do_not_erase_presentation_hints(self):
        for declaration in ('padding:garbage', 'padding:var(--unknown)', 'padding-inline-start:2px',
                            'padding:revert', 'padding:revert-layer'):
            with self.subTest(declaration=declaration):
                root = tree('<table id="t" cellpadding="4"><tr><td id="c">Body</td></tr></table>',
                            '<style>td {' + declaration + '}</style>')
                upgrade_legacy_html(root)
                self.assertEqual(find(root, 't').get('cellpadding'), '4')
                self.assertNotIn('style', find(root, 'c').attrib)

    def test_css_wide_keywords_and_important_do_not_gain_legacy_overrides(self):
        for value in ('initial', 'inherit', 'unset'):
            with self.subTest(value=value):
                root = tree('<table id="t" cellpadding="4"><tr><td id="c">Body</td></tr></table>',
                            '<style>td {padding:' + value + '!important}</style>')
                upgrade_legacy_html(root)
                self.assertNotIn('cellpadding', find(root, 't').attrib)
                self.assertNotIn('style', find(root, 'c').attrib)

    def test_logical_border_does_not_block_unrelated_vertical_alignment(self):
        root = tree('<table><tr><td id="c" valign="top" style="border-inline-start:1px solid">Body</td></tr></table>')
        upgrade_legacy_html(root)
        self.assertNotIn('valign', find(root, 'c').attrib)
        self.assertEqual(declarations(find(root, 'c'))['vertical-align'], 'top')
        self.assertIn('border-inline-start:1px solid', find(root, 'c').get('style'))

    def test_unknown_legacy_values_and_nonzero_border_are_preserved_not_invented(self):
        root = tree('<table id="t" cellpadding="auto" cellspacing="10%" border="2"><tr>'
                    '<td id="c" valign="mystery">Body</td></tr></table>')
        upgrade_legacy_html(root)
        for attr, value in [('cellpadding', 'auto'), ('cellspacing', '10%'), ('border', '2')]:
            self.assertEqual(find(root, 't').get(attr), value)
            self.assertEqual(find(root, 't').get('data-legacy-' + attr), value)
        self.assertEqual(find(root, 'c').get('valign'), 'mystery')
        self.assertNotIn('style', find(root, 'c').attrib)

    def test_svg_math_subtrees_are_untouched_and_upgrade_is_idempotent(self):
        root = tree('<table id="t" cellpadding="0"><col id="col" width="50%"/><tr><td>Body</td></tr></table>'
                    '<svg xmlns="http://www.w3.org/2000/svg" id="svg"><foreignObject>'
                    '<table xmlns="http://www.w3.org/1999/xhtml" cellpadding="4"><col width="20"/>'
                    '<tr><td valign="top">SVG text</td></tr></table></foreignObject></svg>'
                    '<math xmlns="http://www.w3.org/1998/Math/MathML" id="math"><mtable cellpadding="1">'
                    '<mtr><mtd valign="top">x</mtd></mtr></mtable></math>')
        foreign = {id: etree.tostring(find(root, id)) for id in ('svg', 'math')}
        upgrade_legacy_html(root)
        for id, content in foreign.items():
            self.assertEqual(etree.tostring(find(root, id)), content)
        first = etree.tostring(root)
        upgrade_legacy_html(root)
        self.assertEqual(etree.tostring(root), first)


if __name__ == '__main__':
    unittest.main()
