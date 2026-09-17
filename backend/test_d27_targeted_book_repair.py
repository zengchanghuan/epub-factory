import tempfile
import unittest
import zipfile
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from app.domain.epub_targeted_repair import repair_epub


class TargetedRepairTests(unittest.TestCase):
    def test_unchanged_members_and_navigation_targets_are_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'in.epub', Path(tmp)/'out.epub'
            with zipfile.ZipFile(source, 'w') as z:
                z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
                z.writestr('toc.ncx', '<ncx><navMap><navPoint id="x"><navLabel><text>Dedication</text></navLabel><content src="c.xhtml#anchor"/></navPoint></navMap></ncx>')
                z.writestr('c.xhtml', '<html><body><p id="anchor">错误句。</p></body></html>')
                z.writestr('image.jpg', b'unchanged-image-bytes')
            report = repair_epub(source, output, text_edits=[{'file':'c.xhtml','old':'错误句。','new':'正确句。'}])
            self.assertEqual(report['changed_members'], ['c.xhtml','toc.ncx'])
            with zipfile.ZipFile(source) as a, zipfile.ZipFile(output) as b:
                self.assertEqual(a.namelist(), b.namelist())
                self.assertEqual(b.infolist()[0].filename, 'mimetype')
                self.assertEqual(b.infolist()[0].compress_type, zipfile.ZIP_STORED)
                self.assertEqual(a.read('image.jpg'), b.read('image.jpg'))
                self.assertIn(b'src="c.xhtml#anchor"', b.read('toc.ncx'))
                self.assertIn('献词', b.read('toc.ncx').decode())
                self.assertIn(b'id="anchor"', b.read('c.xhtml'))

    def test_stale_or_ambiguous_revision_does_not_create_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'in.epub', Path(tmp)/'out.epub'
            with zipfile.ZipFile(source, 'w') as z: z.writestr('c.xhtml','<p>重复重复</p>')
            with self.assertRaises(ValueError): repair_epub(source, output, text_edits=[{'file':'c.xhtml','old':'重复','new':'修复'}])
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError): repair_epub(source, source)

    def test_explicit_reviewed_repeat_count_is_required_and_version_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'in.epub', Path(tmp)/'out.epub'
            old = '<p>Yes.</p>'
            with zipfile.ZipFile(source, 'w') as z: z.writestr('c.xhtml', '<html><body>'+old*2+'</body></html>')
            edit = {'file':'c.xhtml', 'old':old, 'new':'<p>是的。</p>', 'expected_count':3}
            with self.assertRaises(ValueError): repair_epub(source, output, text_edits=[edit])
            self.assertFalse(output.exists())
            edit['expected_count'] = 2
            repair_epub(source, output, text_edits=[edit])
            with zipfile.ZipFile(output) as z:
                self.assertEqual(z.read('c.xhtml').decode().count('<p>是的。</p>'), 2)

    @unittest.skipUnless(os.environ.get('EPUB_REGRESSION_PREVIOUS_TRANSLATED_BOOK') and os.environ.get('EPUB_REGRESSION_TRANSLATED_BOOK'), 'both selected book versions not provided')
    def test_real_candidate_keeps_every_image_tag_link_id_and_unchanged_member(self):
        with zipfile.ZipFile(os.environ['EPUB_REGRESSION_PREVIOUS_TRANSLATED_BOOK']) as a, \
             zipfile.ZipFile(os.environ['EPUB_REGRESSION_TRANSLATED_BOOK']) as b:
            self.assertEqual(a.namelist(), b.namelist())
            changed = []
            for name in a.namelist():
                before, after = a.read(name), b.read(name)
                if before == after: continue
                changed.append(name)
                self.assertTrue(name.endswith(('.xhtml', '.ncx')))
                left, right = ET.fromstring(before), ET.fromstring(after)
                # Text may change; all markup, media attributes and navigation
                # destinations remain identical, including page/footnote IDs.
                snapshot = lambda root: [(node.tag, sorted(node.attrib.items())) for node in root.iter()]
                self.assertEqual(snapshot(left), snapshot(right), name)
            self.assertEqual(len(changed), 7)
            self.assertEqual(len(a.namelist()) - len(changed), 50)


if __name__ == '__main__': unittest.main()
