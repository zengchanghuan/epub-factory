"""Native PDF paragraph/code regression discovered by the real Python sample."""
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
from app.domain.pdf.local import inspect_pdf


def layout_fixture(path):
    w=PdfWriter();p=w.add_blank_page(500,650)
    fonts={}
    for key,name in [('F1','Helvetica'),('F2','Courier'),('F3','Helvetica-Bold')]:
        f=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/'+name),NameObject('/Encoding'):NameObject('/WinAnsiEncoding')})
        fonts[NameObject('/'+key)]=w._add_object(f)
    p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject(fonts)})
    commands=[
        b'BT /F3 18 Tf 30 600 Td (Values and types) Tj ET',
        b'BT /F1 12 Tf 30 565 Td (A faithful translation preserves the exact original meaning and) Tj 0 -16 Td (keeps every word in this paragraph.) Tj ET',
        rb"BT /F2 12 Tf 30 510 Td (>>> type\(2\)) Tj 0 -16 Td (<class 'int'>) Tj ET",
        b'BT /F1 12 Tf 30 458 Td (This is another paragraph after the code example.) Tj ET',
    ]
    stream=DecodedStreamObject();stream.set_data(b'\n'.join(commands));p[NameObject('/Contents')]=w._add_object(stream)
    w.write(path);return path


class NativeLayoutTests(unittest.TestCase):
    def test_monospaced_prose_is_not_excluded_from_translation(self):
        with tempfile.TemporaryDirectory() as d:
            path=layout_fixture(Path(d)/'s.pdf')
            w=PdfWriter(clone_from=path)
            stream=DecodedStreamObject()
            stream.set_data(b'BT /F2 12 Tf 30 560 Td (This is ordinary prose printed in a monospaced font.) Tj ET')
            w.pages[0][NameObject('/Contents')]=w._add_object(stream);w.write(path)
            blocks=inspect_pdf(path)['pages'][0]['blocks']
            self.assertEqual([b['type'] for b in blocks],['text'])

    def test_native_code_heading_and_paragraph_are_separate_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            info=inspect_pdf(layout_fixture(Path(d)/'s.pdf'));p=info['pages'][0]
            self.assertEqual(p['route'],'local')
            blocks=p['blocks'];kinds=[b['type'] for b in blocks]
            self.assertEqual(kinds,['heading','text','code','text'])
            self.assertIn('meaning and keeps every word',blocks[1]['text'])
            self.assertEqual(blocks[2]['text'],">>> type(2)\n<class 'int'>")
            self.assertIn('\n',blocks[1]['source_text'])
            self.assertEqual(blocks[1]['source_line_indices'],[1,2])

if __name__=='__main__':unittest.main()
