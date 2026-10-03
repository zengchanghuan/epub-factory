"""Offline synthetic contracts for generic PDF reflow (not a visual-quality score)."""
from contextlib import ExitStack
from io import BytesIO
import hashlib
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile
import zlib

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, EncodedStreamObject, DictionaryObject, Fit,
                          NameObject, NullObject, NumberObject, TextStringObject)

from app.domain import pdf_reflow as core


BODY = "Ordinary source text is preserved without editing its content."


def fixture(pages=None, *, outline=(), extra=b"", clip=None, language=None, encrypted=None):
    """Actual, minimal PDFs. Coordinates and text are synthetic, not book rules."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for rows in pages or [[(50, 720, 12, BODY), (50, 700, 12, BODY)]]:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        data = b""
        if clip is not None:
            data += clip
        for x, y, size, text in rows:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            data += f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET\n".encode("latin-1")
        data += extra
        stream = DecodedStreamObject(); stream.set_data(data)
        page[NameObject("/Contents")] = writer._add_object(stream)
    for title, page in outline:
        writer.add_outline_item(title, page, fit=Fit.xyz(0, 800))
    if language is not None:
        writer._root_object[NameObject("/Lang")] = TextStringObject(language)
    writer.add_metadata({"/Title": "Synthetic title <safe>", "/Author": "Synthetic Author"})
    if encrypted is not None:
        writer.encrypt(encrypted)
    target = BytesIO(); writer.write(target)
    return target.getvalue()


def documents(path):
    with zipfile.ZipFile(path) as archive:
        return [(name, ET.fromstring(archive.read(name))) for name in archive.namelist()
                if name.startswith("EPUB/part-") and name.endswith(".xhtml")]


def with_image(data, matrix):
    writer = PdfWriter(); writer.append(PdfReader(BytesIO(data)))
    page = writer.pages[0]
    image = EncodedStreamObject(); image._data = zlib.compress(bytes([255, 0, 0, 0, 0, 255]))
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(2), NameObject("/Height"): NumberObject(1),
                  NameObject("/BitsPerComponent"): NumberObject(8), NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                  NameObject("/Filter"): NameObject("/FlateDecode")})
    page["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject("/I1"): writer._add_object(image)})
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data() + f" q {matrix} cm /I1 Do Q".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO(); writer.write(output)
    return output.getvalue()


def with_links(data, records):
    writer = PdfWriter(); writer.append(PdfReader(BytesIO(data)))
    for page, box, target, x, y in records:
        source = writer.pages[page]
        annotation = DictionaryObject({NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([NumberObject(value) for value in box]),
            NameObject("/Dest"): ArrayObject([writer.pages[target].indirect_reference,
                NameObject("/XYZ"), NumberObject(x), NumberObject(y), NullObject()])})
        source.setdefault(NameObject("/Annots"), ArrayObject()).append(writer._add_object(annotation))
    output = BytesIO(); writer.write(output)
    return output.getvalue()


class PdfReflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="pdf-reflow-unit-")))
        self.source, self.output = self.directory / "source.pdf", self.directory / "result.epub"
        self.guards = [self.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("network forbidden")))
                       for name in ("connect", "connect_ex", "sendto", "sendmsg")]
        self.guards.append(self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))))
        self.addCleanup(lambda: [guard.assert_not_called() for guard in self.guards])

    def run_pdf(self, data=None, **limits):
        self.source.write_bytes(data if data is not None else fixture())
        before = self.source.read_bytes()
        result = core.build_text_pdf(self.source, self.output, **limits)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["output_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        return result

    def reject(self, data, reason, **limits):
        self.source.write_bytes(data)
        with self.assertRaises(core.PdfReflowError) as caught:
            core.build_text_pdf(self.source, self.output, **limits)
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn("Synthetic", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_real_pdf_text_and_page_inventory_preserved(self):
        result = self.run_pdf()
        self.assertEqual(result["schema_version"], "pdf-text-epub-v1")
        self.assertEqual(result["page_count"], 1)
        spans = [n for _, doc in documents(self.output) for n in doc.iter() if n.get("data-pdf-page") and n.get("class") != "pagebreak"]
        self.assertEqual("".join("".join(n.itertext()) for n in spans), BODY * 2)
        self.assertEqual(result["pages"][0]["text_sha256"], hashlib.sha256((BODY * 2).encode()).hexdigest())
        self.assertEqual(result["image_assets"], 0)

    def test_source_metadata_escaped_and_language_not_invented(self):
        self.run_pdf()
        with zipfile.ZipFile(self.output) as archive:
            package = ET.fromstring(archive.read("EPUB/package.opf"))
            self.assertEqual(package.find(".//{http://purl.org/dc/elements/1.1/}title").text, "Synthetic title <safe>")
            self.assertEqual(package.find(".//{http://purl.org/dc/elements/1.1/}language").text, "und")
        self.assertTrue(all(doc.get("lang") == "und" for _, doc in documents(self.output)))

    def test_explicit_valid_language_is_retained(self):
        self.run_pdf(fixture(language="en-GB"))
        self.assertTrue(all(doc.get("lang") == "en-GB" for _, doc in documents(self.output)))

    def test_invalid_language_is_unknown(self):
        self.run_pdf(fixture(language="en<script>"))
        self.assertTrue(all(doc.get("lang") == "und" for _, doc in documents(self.output)))

    def test_output_is_deterministic_and_existing_output_not_overwritten(self):
        self.run_pdf(); first = self.output.read_bytes()
        second = self.directory / "second.epub"
        core.build_text_pdf(self.source, second)
        self.assertEqual(second.read_bytes(), first)
        with self.assertRaises(core.PdfReflowError):
            core.build_text_pdf(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), first)

    def test_page_anchor_and_midpage_outline_keep_preceding_text(self):
        rows = [(50, 720, 12, BODY), (50, 670, 18, "Chapter One"), (50, 640, 12, BODY)]
        self.run_pdf(fixture([rows], outline=[("Chapter One", 0)]))
        docs = documents(self.output)
        self.assertEqual(len(docs), 2)
        self.assertEqual(len([n for _, doc in docs for n in doc.iter() if n.get("id") == "page-1"]), 1)
        self.assertTrue(any(n.get("id") == "page-1" for n in docs[0][1].iter()))
        self.assertTrue(any(n.get("id") == "heading-1" for n in docs[1][1].iter()))
        with zipfile.ZipFile(self.output) as archive:
            nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
            hrefs = [n.get("href") for n in nav.iter() if n.tag.endswith("}a")]
            self.assertIn("part-0000.xhtml#page-1", hrefs)
            self.assertIn("part-0001.xhtml#heading-1", hrefs)

    def test_unresolvable_outline_fails_without_output(self):
        self.reject(fixture(outline=[("Absent title", 0)]), "outline_unresolved")

    def test_latin_wrap_adds_real_marked_space_without_source_mutation(self):
        rows = [(50, 720, 12, BODY[:-1] + " necessary"), (50, 700, 12, "condition follows.")]
        self.run_pdf(fixture([rows]))
        doc = documents(self.output)[0][1]
        self.assertIn("necessary condition", "".join(doc.itertext()))
        generated = [n for n in doc.iter() if n.get("data-generated-spacing")]
        self.assertEqual([n.text for n in generated], [" "])
        spans = [n for n in doc.iter() if n.get("data-pdf-page") and n.get("class") != "pagebreak"]
        self.assertEqual("".join("".join(n.itertext()) for n in spans), "".join(r[3] for r in rows))

    def test_small_late_overlay_is_kept_last_and_audited(self):
        rows = [(50, 720, 12, BODY), (50, 700, 12, BODY), (50, 600, 12, BODY), (80, 690, 9, "Overlay notice")]
        result = self.run_pdf(fixture([rows]))
        self.assertIn("late_text_overlay_preserved", result["warnings"])
        overlays = [n for _, doc in documents(self.output) for n in doc.iter() if n.get("class") == "overlay"]
        self.assertEqual(len(overlays), 1)
        self.assertEqual("".join(overlays[0].itertext()), "Overlay notice")

    def test_column_reset_is_rejected_not_sorted_into_prose(self):
        rows = [(50, 720, 12, BODY), (50, 700, 12, BODY), (350, 720, 12, BODY), (350, 700, 12, BODY)]
        self.reject(fixture([rows]), "layout_unsupported")

    def test_noncutting_rectangular_clip_is_safe(self):
        self.run_pdf(fixture(clip=b"0 0 600 800 re W n\n"))

    def test_transformed_rectangle_clip_is_safe(self):
        self.run_pdf(fixture(clip=b"q 2 0 0 2 0 0 cm 0 0 300 400 re W n Q\n"))

    def test_clip_cutting_text_and_nonrectangular_clip_fail(self):
        for clip in (b"0 0 100 800 re W n\n", b"0 0 m 600 0 l 200 800 l h W n\n",
                     b"0 0 m 600 0 l 0 800 l 600 800 l h W n\n"):
            with self.subTest(clip_type=len(clip)):
                self.reject(fixture(clip=clip), "layout_unsupported")

    def test_invisible_and_clipping_text_modes_rejected(self):
        for mode in (1, 2, 3, 4, 5, 6, 7):
            with self.subTest(mode=mode):
                self.reject(fixture(clip=f"{mode} Tr\n".encode()), "layout_unsupported")

    def test_image_rotation_mirror_shear_are_refused(self):
        for matrix in ("-100 0 0 50 200 500", "0 100 -50 0 200 500", "100 10 0 50 100 500"):
            with self.subTest(transform=matrix.split()[:4]):
                self.reject(with_image(fixture(), matrix), "layout_unsupported")

    def test_positive_anisotropic_image_preserves_pdf_display_ratio_and_pixels(self):
        result = self.run_pdf(with_image(fixture(), "100 0 0 100 100 500"))
        self.assertEqual((result["image_assets"], result["image_placements"]), (1, 1))
        images = [n for _, doc in documents(self.output) for n in doc.iter() if n.tag.endswith("}img")]
        self.assertEqual(len(images), 1)
        self.assertIn("aspect-ratio:100 / 100", images[0].get("style"))
        self.assertIn("object-fit:fill", images[0].get("style"))
        boxes = [n for _, doc in documents(self.output) for n in doc.iter() if "image-box" in n.get("class", "")]
        self.assertIn("65vh", boxes[0].get("style"))
        with zipfile.ZipFile(self.output) as archive:
            from PIL import Image
            with Image.open(BytesIO(archive.read("EPUB/" + images[0].get("src")))) as image:
                self.assertEqual(image.size, (2, 1))
                self.assertEqual(image.tobytes(), bytes([255, 0, 0, 0, 0, 255]))

    def test_source_backed_majority_first_image_becomes_separate_cover(self):
        source = fixture([[], [(50, 720, 12, BODY), (50, 700, 12, BODY)]], outline=[("Cover", 0)])
        self.run_pdf(with_image(source, "450 0 0 650 75 75"))
        docs = documents(self.output)
        self.assertEqual(len(docs), 2)
        self.assertTrue(any(n.get("class") == "cover-page" for n in docs[0][1].iter()))
        self.assertTrue(any(n.get("data-pdf-page-image") == "1" for n in docs[0][1].iter()))
        self.assertFalse(any(n.get("data-pdf-page") == "2" for n in docs[0][1].iter()))
        self.assertTrue(any(n.get("id") == "page-2" for n in docs[1][1].iter()))
        with zipfile.ZipFile(self.output) as archive:
            package = ET.fromstring(archive.read("EPUB/package.opf"))
            covers = [n for n in package.iter() if "cover-image" in n.get("properties", "").split()]
            self.assertEqual(len(covers), 1)
            image = next(n for n in docs[0][1].iter() if n.tag.endswith("}img"))
            self.assertEqual(covers[0].get("href"), image.get("src"))
            css = archive.read("EPUB/styles.css").decode()
            self.assertIn(".cover-page{break-after:page;page-break-after:always}", css)

    def test_small_logo_and_unconfirmed_first_image_not_promoted_to_cover(self):
        for outlined, matrix in ((True, "100 0 0 100 75 75"), (False, "450 0 0 650 75 75")):
            with self.subTest(outline=outlined):
                source = fixture([[], [(50, 720, 12, BODY), (50, 700, 12, BODY)]],
                                 outline=[("Cover", 0)] if outlined else ())
                self.run_pdf(with_image(source, matrix))
                with zipfile.ZipFile(self.output) as archive:
                    package = ET.fromstring(archive.read("EPUB/package.opf"))
                    self.assertFalse(any("cover-image" in n.get("properties", "").split() for n in package.iter()))
                self.output.unlink()

    def test_printed_toc_link_uses_source_outline_heading_not_page_top_guess(self):
        data = fixture([[(50, 720, 12, "Chapter One"), (50, 700, 12, BODY)],
                        [(50, 720, 12, BODY), (50, 650, 18, "Chapter One"), (50, 620, 12, BODY)]],
                       outline=[("Chapter One", 1)])
        self.run_pdf(with_links(data, [(0, (50, 717, 130, 730), 1, 0, 800)]))
        docs = documents(self.output)
        link = next(n for _, doc in docs for n in doc.iter() if n.get("data-pdf-link-id"))
        self.assertEqual("".join(link.itertext()), "Chapter One")
        self.assertEqual(link.get("data-pdf-link-source-page"), "1")
        self.assertEqual(link.get("data-pdf-link-target-page"), "2")
        name, target = link.get("href").split("#")
        destination = next(n for filename, doc in docs if filename == "EPUB/" + name for n in doc.iter() if n.get("id") == target)
        self.assertTrue(destination.tag.endswith("}h1"))
        self.assertEqual("".join(destination.itertext()), "Chapter One")

    def test_existing_footnote_and_return_map_to_exact_source_glyph(self):
        data = fixture([[(50, 720, 12, BODY), (50, 650, 12, "1 Source note")],
                        [(50, 720, 12, BODY), (50, 650, 12, "1 Footnote body")]])
        self.run_pdf(with_links(data, [(0, (50, 647, 57, 660), 1, 50, 660),
                                     (1, (50, 647, 57, 660), 0, 50, 660)]))
        docs = documents(self.output)
        links = [n for _, doc in docs for n in doc.iter() if n.get("data-pdf-link-id")]
        self.assertEqual(len(links), 2)
        self.assertEqual(["".join(n.itertext()) for n in links], ["1", "1"])
        for link in links:
            name, target = link.get("href").split("#")
            self.assertTrue(target.startswith("pdf-target-"))
            matches = [n for filename, doc in docs if filename == "EPUB/" + name for n in doc.iter() if n.get("id") == target]
            self.assertEqual(len(matches), 1)
            self.assertEqual("".join(matches[0].itertext()), "")

    def test_source_outline_entries_break_blocks_when_parent_dedents(self):
        rows = [[(80, 720, 12, "Child entry"), (50, 700, 12, "Parent entry")],
                [(50, 720, 18, "Child entry"), (50, 690, 12, BODY), (50, 670, 12, BODY)],
                [(50, 720, 18, "Parent entry"), (50, 690, 12, BODY), (50, 670, 12, BODY)]]
        data = fixture(rows, outline=[("Child entry", 1), ("Parent entry", 2)])
        self.run_pdf(with_links(data, [(0, (80, 717, 170, 730), 1, 0, 800),
                                     (0, (50, 697, 150, 710), 2, 0, 800)]))
        blocks = [n for _, doc in documents(self.output) for n in doc.iter()
                  if n.tag.endswith(("}p", "}h3")) and any(c.get("data-pdf-link-id") for c in n.iter())]
        self.assertEqual(["".join(n.itertext()) for n in blocks], ["Child entry", "Parent entry"])
        self.assertEqual([{c.get("data-pdf-link-id") for c in n.iter() if c.get("data-pdf-link-id")}
                          for n in blocks], [{"1"}, {"2"}])

    def test_one_wrapped_source_outline_entry_stays_in_one_block(self):
        rows = [[(80, 720, 12, "Long source"), (80, 700, 12, "entry continued"), (50, 680, 12, BODY)],
                [(50, 720, 18, "Long source entry continued"), (50, 690, 12, BODY), (50, 670, 12, BODY)]]
        data = fixture(rows, outline=[("Long source entry continued", 1)])
        self.run_pdf(with_links(data, [(0, (80, 697, 170, 730), 1, 0, 800)]))
        blocks = [n for _, doc in documents(self.output) for n in doc.iter()
                  if n.tag.endswith(("}p", "}h3")) and any(c.get("data-pdf-link-id") for c in n.iter())]
        self.assertEqual(len(blocks), 1)
        self.assertEqual("".join(blocks[0].itertext()), "Long source entry continued")
        self.assertEqual(len([n for n in blocks[0].iter() if n.get("data-pdf-link-id") == "1"]), 2)
        self.assertNotIn(BODY, "".join(blocks[0].itertext()))

    def test_plain_hanging_indent_and_partial_outline_link_keep_prose_flow(self):
        for linked in (False, True):
            with self.subTest(partial_outline_link=linked):
                rows = [[(80, 720, 12, "Chapter One starts this ordinary paragraph"),
                         (50, 700, 12, "and this line continues the paragraph.")],
                        [(50, 720, 18, "Chapter One"), (50, 690, 12, BODY), (50, 670, 12, BODY)]]
                data = fixture(rows, outline=[("Chapter One", 1)])
                if linked:
                    data = with_links(data, [(0, (80, 717, 151, 730), 1, 0, 800)])
                self.run_pdf(data)
                blocks = [n for _, doc in documents(self.output) for n in doc.iter()
                          if n.tag.endswith("}p") and "starts this ordinary" in "".join(n.itertext())]
                self.assertEqual(len(blocks), 1)
                self.assertEqual("".join(blocks[0].itertext()), rows[0][0][3] + " " + rows[0][1][3])
                self.assertIsNone(blocks[0].get("data-pdf-toc-entry"))
                self.output.unlink()

    def test_ambiguous_or_missing_link_target_is_not_downgraded_to_page_start(self):
        rows = [[(50, 720, 12, BODY), (50, 650, 12, "1 Source note")],
                [(50, 720, 12, BODY), (50, 650, 12, "1"), (50, 650, 12, "1")]]
        self.reject(with_links(fixture(rows), [(0, (50, 647, 57, 660), 1, 50, 660)]), "layout_unsupported")
        self.reject(with_links(fixture(), [(0, (50, 717, 60, 730), 0, 450, 400)]), "layout_unsupported")
        # Matching label/page is insufficient if the original XYZ destination
        # disagrees with the source outline's destination.
        data = fixture([[(50, 720, 12, "Chapter One"), (50, 700, 12, BODY)],
                        [(50, 720, 18, "Chapter One"), (50, 690, 12, BODY)]], outline=[("Chapter One", 1)])
        self.reject(with_links(data, [(0, (50, 717, 130, 730), 1, 450, 400)]), "layout_unsupported")

    def test_partial_glyph_rect_and_external_action_fail_without_output(self):
        self.reject(with_links(fixture(), [(0, (51, 717, 54, 730), 0, 50, 730)]), "layout_unsupported")
        writer = PdfWriter(); writer.append(PdfReader(BytesIO(fixture())))
        annotation = DictionaryObject({NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([NumberObject(v) for v in (50, 717, 130, 730)]),
            NameObject("/A"): DictionaryObject({NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject("https://invalid.example/private")})})
        writer.pages[0][NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
        data = BytesIO(); writer.write(data)
        self.reject(data.getvalue(), "layout_unsupported")

    def test_vector_fraction_is_not_silently_changed_to_horizontal_rule(self):
        self.reject(fixture([[(50, 720, 12, BODY), (100, 650, 12, "A"), (100, 625, 12, "B")]],
                            extra=b"95 645 m 125 645 l S"), "layout_unsupported")

    def test_optional_content_layer_fails_closed(self):
        writer = PdfWriter(); writer.append(PdfReader(BytesIO(fixture())))
        writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
            NameObject("/OCGs"): ArrayObject(), NameObject("/D"): DictionaryObject({NameObject("/BaseState"): NameObject("/OFF")})})
        data = BytesIO(); writer.write(data)
        self.reject(data.getvalue(), "layout_unsupported")
        self.reject(fixture(extra=b"/OC /Hidden BDC BT /F1 12 Tf 1 0 0 1 50 650 Tm (Hidden layer) Tj ET EMC"), "layout_unsupported")

    def test_white_and_rgb_text_color_preserved_not_replaced_by_black(self):
        self.run_pdf(fixture(extra=b"1 g BT /F1 12 Tf 1 0 0 1 50 650 Tm (White text.) Tj ET 0 0 1 rg BT /F1 12 Tf 1 0 0 1 50 625 Tm (Blue text.) Tj ET"))
        styles = [n.get("style") for _, doc in documents(self.output) for n in doc.iter() if n.get("style")]
        self.assertIn("color:rgb(100%,100%,100%)", styles)
        self.assertIn("color:rgb(0%,0%,100%)", styles)

    def test_generated_unknown_cid_glyph_refused_but_literal_text_retained(self):
        writer = PdfWriter(); page = writer.add_blank_page(width=600, height=800)
        descriptor = DictionaryObject({NameObject("/Type"): NameObject("/FontDescriptor"), NameObject("/FontName"): NameObject("/Dummy"),
            NameObject("/Flags"): NumberObject(4), NameObject("/FontBBox"): ArrayObject([NumberObject(v) for v in (0, -200, 1000, 800)]),
            **{NameObject(k): NumberObject(v) for k, v in {"/ItalicAngle": 0, "/Ascent": 800, "/Descent": -200, "/CapHeight": 700, "/StemV": 80}.items()}})
        descendant = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/CIDFontType2"),
            NameObject("/BaseFont"): NameObject("/Dummy"), NameObject("/DW"): NumberObject(600),
            NameObject("/FontDescriptor"): writer._add_object(descriptor), NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"), NameObject("/Ordering"): TextStringObject("Identity"), NameObject("/Supplement"): NumberObject(0)})})
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): NameObject("/Dummy"), NameObject("/Encoding"): NameObject("/Identity-H"),
            NameObject("/DescendantFonts"): ArrayObject([writer._add_object(descendant)])})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 1 0 0 1 50 720 Tm <" + b"0001" * 30 + b"> Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO(); writer.write(output)
        self.reject(output.getvalue(), "invalid_text")
        self.run_pdf(fixture([[(50, 720, 12, BODY + " (cid:1)")]]))

    def test_horizontal_separator_retained_and_arbitrary_curve_rejected(self):
        self.run_pdf(fixture(extra=b"50 650 m 420 650 l S"))
        self.assertTrue(any(n.tag.endswith("}hr") for _, doc in documents(self.output) for n in doc.iter()))
        self.output.unlink()
        self.reject(fixture(extra=b"50 650 m 100 680 200 600 420 650 c S"), "layout_unsupported")

    def test_thin_double_rule_caps_supported_not_rectangular_diagram(self):
        self.run_pdf(fixture(extra=b"50 650 370 0.5 re f 50 652 370 0.5 re f 50 650 m 50.5 650 l 50.5 652.5 l 50 652.5 l h f"))
        self.assertTrue(any(n.tag.endswith("}hr") for _, doc in documents(self.output) for n in doc.iter()))
        self.output.unlink()
        self.reject(fixture(extra=b"50 600 100 40 re f"), "layout_unsupported")

    def test_limits_are_checked_before_output(self):
        for kwargs in ({"max_pages": 0}, {"max_pages": True}, {"max_pages": 501}, {"max_total_chars": 3}, {"max_page_chars": 3}):
            with self.subTest(limit=tuple(kwargs)):
                self.reject(fixture(), "input_limit", **kwargs)
        self.reject(fixture([[(50, 720, 12, BODY)], [(50, 720, 12, BODY)]]), "input_limit", max_pages=1)

    def test_blank_and_corrupt_sources_do_not_create_epub(self):
        self.reject(fixture([[]]), "text_missing")
        self.reject(b"not a PDF", "invalid_input")

    def test_password_protected_pdf_is_rejected(self):
        self.reject(fixture(encrypted="required-password"), "encrypted_pdf")

    def test_unrepresentable_text_refused_ordinary_math_allowed(self):
        for text in ("\ufffd", "\ue001", "\U000f0001", "\u0000", "\u202e"):
            self.assertFalse(core._text_ok(text))
        self.assertTrue(core._text_ok("A∧B→C≤D\u200b\n\t"))

    def test_write_failure_removes_only_own_partial_file(self):
        self.source.write_bytes(fixture())
        with patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("private text")):
            with self.assertRaises(core.PdfReflowError) as caught:
                core.build_text_pdf(self.source, self.output)
        self.assertEqual(caught.exception.reason, "invalid_output")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
