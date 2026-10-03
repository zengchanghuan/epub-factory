"""B1 synthetic IR contracts: raw PDF fragments/resources, never paragraphs.

All PDF bytes are disposable fixtures, not real manuscripts or deliverables.
No app startup, public input, database, payment or model is imported. The parser
uses its own isolated -I -B child; run this suite outside release_guard.
"""
import base64
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
from dataclasses import replace
import hashlib
from io import BytesIO, StringIO
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject

from app.domain import pdf_text_ir as ir
from test_d56_pdf_text_preflight import CompletedWorker, fixture_pdf


RAW_LINES = ("IR_SYNTHETIC_RAW_LINE_ONE", "IR_SYNTHETIC_RAW_LINE_TWO")
PIXELS = b"\xff\x00\x00"
ENCODED_IMAGE = zlib.compress(PIXELS)


def ir_fixture(*, pages=1, images=False, separate_images=False, zero_width=False, metadata=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    if zero_width:
        cmap = DecodedStreamObject()
        cmap.set_data(b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
                      b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
                      b"/CMapName /SyntheticZeroWidth def /CMapType 2 def "
                      b"1 begincodespacerange <00> <FF> endcodespacerange "
                      b"1 beginbfchar <5A> <200B> endbfchar "
                      b"endcmap CMapName currentdict /CMap defineresource pop end end")
        font[NameObject("/ToUnicode")] = writer._add_object(cmap)
    font_ref = writer._add_object(font)
    image_ref = None
    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        text = "AZB" if zero_width else RAW_LINES[0]
        operations = ["BT /F1 12 Tf 1 0 0 1 72 720 Tm (" + text + ") Tj 0 -24 Td (" + RAW_LINES[1] + ") Tj ET"]
        if images:
            if image_ref is None or separate_images:
                image = DecodedStreamObject()
                # This is deliberately a filtered encoded stream. The IR must
                # retain its encoded bytes, not get_data() or a generated PNG.
                image.set_data(ENCODED_IMAGE)
                image.update({NameObject("/Type"): NameObject("/XObject"),
                              NameObject("/Subtype"): NameObject("/Image"),
                              NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                              NameObject("/BitsPerComponent"): NumberObject(8),
                              NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                              NameObject("/Filter"): NameObject("/FlateDecode")})
                image_ref = writer._add_object(image)
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/I1"): image_ref})
            operations.append("q 200 0 0 200 72 400 cm /I1 Do Q /I1 Do")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(operations).encode())
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(stream)
    if metadata:
        writer.add_metadata({"/Title": "Synthetic title", "/Author": "Synthetic author"})
        writer.add_outline_item("Synthetic chapter", 0)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def alternate_image_carrier_fixture(carrier, *, external_key=None):
    """Each unsupported carrier contains an actual nested image stream."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    image = DecodedStreamObject()
    image.set_data(ENCODED_IMAGE)
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                  NameObject("/BitsPerComponent"): NumberObject(8), NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                  NameObject("/Filter"): NameObject("/FlateDecode")})
    nested = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/I1"): writer._add_object(image)})})
    box = ArrayObject([NumberObject(n) for n in (0, 0, 10, 10)])
    stream = DecodedStreamObject()
    stream.set_data(b"/I1 Do")
    stream.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                   NameObject("/BBox"): box, NameObject("/Resources"): nested})
    resource = writer._add_object(stream)
    resources, content = DictionaryObject(), b""
    if carrier == "pattern":
        stream.pop(NameObject("/Subtype"))
        stream[NameObject("/Type")] = NameObject("/Pattern")
        for key, value in (("/PatternType", 1), ("/PaintType", 1), ("/TilingType", 1), ("/XStep", 10), ("/YStep", 10)):
            stream[NameObject(key)] = NumberObject(value)
        resources[NameObject("/Pattern")] = DictionaryObject({NameObject("/P1"): resource})
        content = b"/Pattern cs /P1 scn 0 0 30 30 re f"
    elif carrier == "annotation_appearance":
        annotation = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Stamp"),
                                       NameObject("/Rect"): box, NameObject("/AP"): DictionaryObject({NameObject("/N"): resource})})
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    elif carrier == "type3_charproc":
        stream.set_data(b"500 0 d0 /I1 Do")
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type3"),
                                 NameObject("/FontBBox"): box,
                                 NameObject("/FontMatrix"): ArrayObject([FloatObject(n) for n in (0.001, 0, 0, 0.001, 0, 0)]),
                                 NameObject("/CharProcs"): DictionaryObject({NameObject("/A"): resource}),
                                 NameObject("/Resources"): nested,
                                 NameObject("/Encoding"): DictionaryObject({NameObject("/Type"): NameObject("/Encoding"),
                                     NameObject("/Differences"): ArrayObject([NumberObject(65), NameObject("/A")])}),
                                 NameObject("/FirstChar"): NumberObject(65), NameObject("/LastChar"): NumberObject(65),
                                 NameObject("/Widths"): ArrayObject([NumberObject(500)])})
        resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        content = b"BT /F1 12 Tf 72 720 Td (A) Tj ET"
    elif carrier == "soft_mask_group":
        stream[NameObject("/Group")] = DictionaryObject({NameObject("/S"): NameObject("/Transparency"), NameObject("/CS"): NameObject("/DeviceRGB")})
        state = DictionaryObject({NameObject("/Type"): NameObject("/ExtGState"),
                                  NameObject("/SMask"): DictionaryObject({NameObject("/S"): NameObject("/Luminosity"), NameObject("/G"): resource})})
        resources[NameObject("/ExtGState")] = DictionaryObject({NameObject("/GS1"): writer._add_object(state)})
        content = b"/GS1 gs 0 0 30 30 re f"
    elif carrier == "external_form":
        values = {"/Ref": DictionaryObject({NameObject("/F"): TextStringObject("private-external.pdf"), NameObject("/Page"): NumberObject(0)}),
                  "/F": TextStringObject("private-external.pdf"), "/FFilter": NameObject("/FlateDecode"),
                  "/FDecodeParms": DictionaryObject({NameObject("/Columns"): NumberObject(3)})}
        stream[NameObject(external_key)] = values[external_key]
        resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fm1"): resource})
        content = b"/Fm1 Do"
    else:
        raise AssertionError("unknown fixture carrier")
    page[NameObject("/Resources")] = resources
    contents = DecodedStreamObject()
    contents.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(contents)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class PdfTextIrTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="d57-synthetic-"))).resolve()
        self.path = self.write("private-source.pdf", ir_fixture())
        self.guards = [self.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("network forbidden")))
                       for name in ("connect", "connect_ex")]
        self.guards.append(self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))))
        self.addCleanup(lambda: [guard.assert_not_called() for guard in self.guards])

    def write(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        return path

    def reject(self, path=None, reason=None, **kwargs):
        with self.assertRaises(ir.PdfIrError) as caught:
            ir.extract_pdf_text_ir(path or self.path, **kwargs)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn(RAW_LINES[0], str(caught.exception))
        return caught.exception

    def inject(self, value=None, *, raw=None, exit_code=0):
        def launch(snapshot, result_path, limits):
            Path(result_path).write_bytes(raw if raw is not None else json.dumps(value).encode())
            process = CompletedWorker()
            process.returncode = exit_code
            return process
        return patch.object(ir, "_launch_worker", side_effect=launch)

    def test_raw_text_matches_independent_parser_without_line_to_paragraph_conversion(self):
        before = self.path.read_bytes()
        reference = PdfReader(BytesIO(before), strict=True).pages[0].extract_text()
        result = ir.extract_pdf_text_ir(self.path)
        self.assertEqual(result["schema_version"], "pdf-text-ir-v1")
        self.assertEqual(result["parser_version"], "6.0.0")
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["status"], "review_required")
        self.assertIs(result["eligible_for_payment"], False)
        self.assertEqual(result["page_count"], 1)
        page = result["pages"][0]
        self.assertEqual(page["raw_text"], reference)
        self.assertEqual(page["char_count"], len(reference))
        self.assertEqual(page["nonspace_count"], sum(not ch.isspace() for ch in reference))
        self.assertNotIn("paragraphs", page)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {self.path.name})

    def test_fragments_are_raw_ordered_visitor_events_with_stable_identity(self):
        reference = []
        def visitor(text, cm, tm, font, size):
            if text:
                reference.append(text)
        PdfReader(self.path, strict=True).pages[0].extract_text(visitor_text=visitor)
        first = ir.extract_pdf_text_ir(self.path)
        second = ir.extract_pdf_text_ir(self.path)
        self.assertEqual(first, second)
        fragments = first["pages"][0]["fragments"]
        self.assertEqual([fragment["text"] for fragment in fragments], reference)
        self.assertEqual(len({fragment["fragment_id"] for fragment in fragments}), len(fragments))
        self.assertEqual([fragment["sequence"] for fragment in fragments], sorted(fragment["sequence"] for fragment in fragments))
        self.assertEqual(first["total_fragment_count"], len(fragments))
        for fragment in fragments:
            self.assertTrue(fragment["fragment_id"])
            self.assertEqual(len(fragment["cm"]), 6)
            self.assertEqual(len(fragment["tm"]), 6)
            self.assertEqual(fragment["font_size"], 12)

    def test_zero_width_characters_are_not_silently_dropped(self):
        path = self.write("zero-width.pdf", ir_fixture(zero_width=True))
        original = PdfReader(path, strict=True).pages[0].extract_text()
        self.assertIn("\u200b", original)
        result = ir.extract_pdf_text_ir(path)
        self.assertEqual(result["pages"][0]["raw_text"], original)
        self.assertIn("\u200b", "".join(fragment["text"] for fragment in result["pages"][0]["fragments"]))

    def test_image_resource_retains_encoded_bytes_hash_and_filter_without_transcoding(self):
        path = self.write("image.pdf", ir_fixture(images=True))
        result = ir.extract_pdf_text_ir(path)
        self.assertEqual(len(result["resources"]), 1)
        resource = result["resources"][0]
        self.assertEqual(base64.b64decode(resource["encoded_stream_base64"], validate=True), ENCODED_IMAGE)
        self.assertNotEqual(ENCODED_IMAGE, PIXELS)
        self.assertEqual(resource["encoded_stream_sha256"], hashlib.sha256(ENCODED_IMAGE).hexdigest())
        self.assertEqual(resource["encoded_size"], len(ENCODED_IMAGE))
        self.assertEqual(resource["filters"], ["/FlateDecode"])
        self.assertIn("encoded_stream_only", resource["flags"])
        self.assertIn("opaque_filter", resource["flags"])
        self.assertEqual((resource["width"], resource["height"], resource["bits_per_component"]), (1, 1, 8))
        self.assertEqual(result["pages"][0]["image_resource_ids"], [resource["resource_id"]])
        self.assertEqual(result["total_encoded_resource_bytes"], len(ENCODED_IMAGE))

    def test_shared_object_is_deduplicated_across_pages_not_counted_as_draw_calls(self):
        result = ir.extract_pdf_text_ir(self.write("shared.pdf", ir_fixture(pages=2, images=True)))
        self.assertEqual(len(result["resources"]), 1)
        identity = result["resources"][0]["resource_id"]
        self.assertEqual([p["image_resource_ids"] for p in result["pages"]], [[identity], [identity]])
        self.assertEqual(len({p["page_id"] for p in result["pages"]}), 2)

    def test_equal_encoded_bytes_in_distinct_objects_preserve_distinct_resource_identities(self):
        result = ir.extract_pdf_text_ir(self.write("distinct.pdf", ir_fixture(pages=2, images=True, separate_images=True)))
        self.assertEqual(len(result["resources"]), 2)
        self.assertEqual(len({r["resource_id"] for r in result["resources"]}), 2)
        self.assertEqual(len({r["encoded_stream_sha256"] for r in result["resources"]}), 1)

    def test_blank_and_image_only_pages_stay_locatable_without_fake_text(self):
        result = ir.extract_pdf_text_ir(self.write("mixed.pdf", fixture_pdf(("text", "blank", "image"))))
        self.assertEqual([p["page_number"] for p in result["pages"]], [1, 2, 3])
        for page in result["pages"][1:]:
            self.assertEqual(page["raw_text"], "")
            self.assertEqual(page["fragments"], [])
            self.assertTrue(page["page_id"])
        self.assertEqual(result["pages"][1]["image_resource_ids"], [])
        self.assertEqual(len(result["pages"][2]["image_resource_ids"]), 1)

    def test_metadata_outline_and_source_bound_identities_are_preserved(self):
        first = ir.extract_pdf_text_ir(self.path)
        second = ir.extract_pdf_text_ir(self.write("metadata.pdf", ir_fixture(metadata=True)))
        self.assertEqual(second["metadata"], {"title": "Synthetic title", "author": "Synthetic author"})
        self.assertEqual(second["outline"][0]["title"], "Synthetic chapter")
        self.assertEqual(second["outline"][0]["page_number"], 1)
        self.assertIsInstance(second["outline"][0]["depth"], int)
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])
        self.assertNotEqual(first["pages"][0]["page_id"], second["pages"][0]["page_id"])
        self.assertNotEqual(first["pages"][0]["fragments"][0]["fragment_id"], second["pages"][0]["fragments"][0]["fragment_id"])

    def test_unsupported_inline_image_refuses_whole_ir_instead_of_silently_omitting_it(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(b"q BI /W 1 /H 1 /CS /RGB /BPC 8 ID\n\xff\x00\x00\nEI Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO()
        writer.write(output)
        path = self.write("inline.pdf", output.getvalue())
        self.reject(path, "unsupported_content")
        self.assertEqual(path.read_bytes(), output.getvalue())

    def test_alternate_nested_image_carriers_refuse_instead_of_returning_incomplete_resource_ir(self):
        for carrier in ("pattern", "annotation_appearance", "type3_charproc", "soft_mask_group"):
            content = alternate_image_carrier_fixture(carrier)
            with self.subTest(carrier=carrier):
                path = self.write(carrier + ".pdf", content)
                self.reject(path, "unsupported_content")
                self.assertEqual(path.read_bytes(), content)

    def test_external_form_dependencies_are_refused_without_reading_or_fetching_them(self):
        for key in ("/Ref", "/F", "/FFilter", "/FDecodeParms"):
            content = alternate_image_carrier_fixture("external_form", external_key=key)
            with self.subTest(key=key):
                self.reject(self.write(key[1:] + ".pdf", content), "unsupported_content")

    def test_invalid_limits_refuse_boolean_nonfinite_or_unbounded_values_before_launch(self):
        fields = ("max_file_bytes", "max_pages", "max_page_chars", "max_total_chars", "max_fragments_per_page",
                  "max_total_fragments", "max_resources", "max_resource_bytes", "max_total_resource_bytes", "max_result_bytes")
        with patch.object(ir, "_launch_worker") as launch:
            for name in fields:
                for value in (0, True, -1, 1.5, 1 << 63):
                    with self.subTest(name=name, value=value):
                        self.reject(reason="invalid_limits", limits=replace(ir.PdfIrLimits(), **{name: value}))
            for value in (0, True, float("nan"), float("inf")):
                self.reject(reason="invalid_limits", limits=replace(ir.PdfIrLimits(), timeout_seconds=value))
            launch.assert_not_called()

    def test_source_symlink_and_parent_symlink_never_become_readable_ir(self):
        alias = self.root / "link.pdf"
        alias.symlink_to(self.path)
        parent = self.root / "linked-directory"
        parent.symlink_to(self.root, target_is_directory=True)
        for source in (alias, parent / self.path.name, self.root / "missing.pdf", self.root):
            with self.subTest(name=source.name):
                self.reject(source, "invalid_source")

    def test_ir_preserves_empty_password_permission_gate_without_unlocking_protected_books(self):
        self.reject(self.write("password.pdf", fixture_pdf(encrypted="synthetic-password")), "encrypted_pdf")
        self.reject(self.write("no-extract.pdf", fixture_pdf(encrypted="", permissions=UserAccessPermissions.PRINT)),
                    "extraction_forbidden")
        result = ir.extract_pdf_text_ir(self.write("allowed.pdf", fixture_pdf(encrypted="", permissions=UserAccessPermissions.EXTRACT)))
        self.assertIn("empty_password_encryption", result["flags"])
        self.assertEqual(result["status"], "review_required")
        self.assertIs(result["eligible_for_payment"], False)

    def test_real_limits_stop_before_returning_partial_fragments_or_resources(self):
        path = self.write("bounded.pdf", ir_fixture(pages=2, images=True, separate_images=True))
        cases = (
            ({"max_file_bytes": 1}, "file_limit"), ({"max_pages": 1}, "page_limit"),
            # The visitor enforces the shared character cap before extract_text
            # returns its complete raw text, so these stop as fragment limits.
            ({"max_page_chars": 1}, "fragment_limit"), ({"max_total_chars": 1}, "fragment_limit"),
            ({"max_fragments_per_page": 1}, "fragment_limit"), ({"max_total_fragments": 1}, "fragment_limit"),
            ({"max_resources": 1}, "resource_limit"), ({"max_resource_bytes": 1}, "resource_limit"),
            ({"max_total_resource_bytes": len(ENCODED_IMAGE)}, "resource_limit"),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                self.reject(path, reason, limits=replace(ir.PdfIrLimits(), **changes))

    def test_parent_rejects_extra_resource_paths_and_untrusted_metadata_fields(self):
        good = ir.extract_pdf_text_ir(self.write("resource.pdf", ir_fixture(images=True)))
        for key, value in (("path", "../../private"), ("file_path", "/private/secret"), ("url", "https://example.invalid/private")):
            result = copy.deepcopy(good)
            result["resources"][0][key] = value
            with self.subTest(key=key), self.inject(result):
                self.reject(self.root / "resource.pdf", "invalid_result")
        result = copy.deepcopy(good)
        result["metadata"]["private_path"] = str(self.root)
        with self.inject(result):
            self.reject(self.root / "resource.pdf", "invalid_result")

    def test_parent_rejects_broken_resource_hash_base64_reference_or_size(self):
        path = self.write("resource.pdf", ir_fixture(images=True))
        good = ir.extract_pdf_text_ir(path)
        mutations = (
            lambda r: r["resources"][0].update(encoded_stream_sha256="0" * 64),
            lambda r: r["resources"][0].update(encoded_stream_base64="!not-base64!"),
            lambda r: r["resources"][0].update(encoded_size=True),
            lambda r: r["pages"][0].update(image_resource_ids=["not-a-resource"]),
            lambda r: r.update(total_encoded_resource_bytes=0),
            lambda r: r["resources"].append(copy.deepcopy(r["resources"][0])),
        )
        for index, mutate in enumerate(mutations):
            result = copy.deepcopy(good)
            mutate(result)
            with self.subTest(case=index), self.inject(result):
                self.reject(path, "invalid_result")

    def test_parent_rejects_partial_pages_bad_fragment_counts_and_nonfinite_matrices(self):
        path = self.write("two.pdf", ir_fixture(pages=2))
        good = ir.extract_pdf_text_ir(path)
        mutations = (
            lambda r: r["pages"].pop(),
            lambda r: r["pages"][1].update(page_number=1),
            lambda r: r["pages"][1].update(page_id=r["pages"][0]["page_id"]),
            lambda r: r["pages"][0]["fragments"][0].update(cm=[float("nan")] * 6),
            lambda r: r["pages"][0]["fragments"][0].update(tm=[1, 2]),
            lambda r: r["pages"][0].update(char_count=True),
            lambda r: r.update(total_fragment_count=0),
            lambda r: r.update(eligible_for_payment=True),
            lambda r: r.update(status="approved"),
        )
        for index, mutate in enumerate(mutations):
            result = copy.deepcopy(good)
            mutate(result)
            with self.subTest(case=index), self.inject(result):
                self.reject(path, "invalid_result")

    def test_invalid_or_oversized_json_and_failed_worker_do_not_return_partial_ir(self):
        for raw in (b"{", b"null", b"[]", b'{"error":"corrupt_pdf","error":"timeout"}'):
            with self.subTest(raw=raw), self.inject(raw=raw):
                self.reject(reason="invalid_result")
        with self.inject(raw=b" " * 2048):
            self.reject(reason="result_limit", limits=replace(ir.PdfIrLimits(), max_result_bytes=1024))
        good = ir.extract_pdf_text_ir(self.path)
        with self.inject(good, exit_code=3):
            self.reject(reason="worker_failed")

    def test_cancellation_before_launch_returns_no_ir(self):
        with patch.object(ir, "_launch_worker") as launch:
            self.reject(reason="cancelled", cancel_check=lambda: True)
            launch.assert_not_called()

    def test_cancel_during_parent_resource_validation_rejects_already_completed_child(self):
        path = self.write("parent-cancel.pdf", ir_fixture(images=True))
        good = ir.extract_pdf_text_ir(path)
        decoded = False
        original_decode = base64.b64decode
        def cancel_after_decode(*args, **kwargs):
            nonlocal decoded
            value = original_decode(*args, **kwargs)
            decoded = True
            return value
        # inject() returns a worker whose poll() is already zero. Cancellation
        # becomes true only inside parent-side resource validation, not wait().
        with self.inject(good), patch.object(ir.base64, "b64decode", side_effect=cancel_after_decode) as decoder:
            self.reject(path, "cancelled", cancel_check=lambda: decoded)
        self.assertTrue(decoded)
        decoder.assert_called_once()

    def test_deadline_expiring_during_parent_json_parse_rejects_completed_child(self):
        good = ir.extract_pdf_text_ir(self.path)
        clock = {"now": 1000.0, "parsed": False}
        original_loads = json.loads
        def finish_json_after_deadline(*args, **kwargs):
            value = original_loads(*args, **kwargs)
            clock.update(now=1061.0, parsed=True)
            return value
        # No sleeping or slow-machine assumption: the controlled clock advances
        # past the 60s deadline only after the valid child result is parsed.
        with self.inject(good), patch.object(ir.time, "monotonic", side_effect=lambda: clock["now"]), \
                patch.object(ir.json, "loads", side_effect=finish_json_after_deadline) as parser:
            self.reject(reason="timeout", limits=replace(ir.PdfIrLimits(), timeout_seconds=60))
        self.assertTrue(clock["parsed"])
        parser.assert_called_once()

    def test_cancel_after_final_validation_or_read_completion_is_checked_before_return(self):
        good = ir.extract_pdf_text_ir(self.path)
        for seam in ("_validate_result", "_read_result"):
            finished = {"value": False}
            original = getattr(ir, seam)
            def completed_then_cancel(*args, **kwargs):
                value = original(*args, **kwargs)
                finished["value"] = True
                return value
            with self.subTest(seam=seam), self.inject(good), patch.object(ir, seam, side_effect=completed_then_cancel):
                self.reject(reason="cancelled", cancel_check=lambda: finished["value"])
            self.assertTrue(finished["value"])

    def test_timeout_after_final_validation_or_read_completion_is_checked_before_return(self):
        good = ir.extract_pdf_text_ir(self.path)
        for seam in ("_validate_result", "_read_result"):
            clock = {"now": 1000.0, "finished": False}
            original = getattr(ir, seam)
            def completed_then_expire(*args, **kwargs):
                value = original(*args, **kwargs)
                clock.update(now=1061.0, finished=True)
                return value
            with self.subTest(seam=seam), self.inject(good), patch.object(ir, seam, side_effect=completed_then_expire), \
                    patch.object(ir.time, "monotonic", side_effect=lambda: clock["now"]):
                self.reject(reason="timeout", limits=replace(ir.PdfIrLimits(), timeout_seconds=60))
            self.assertTrue(clock["finished"])

    def sleeping_worker(self, snapshot, result_path, limits):
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        self.stack.callback(self.stop_process, process)
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    def test_timeout_and_midflight_cancel_stop_only_owned_worker(self):
        sibling = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"], start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.stack.callback(self.stop_process, sibling)
        for cancelled in (False, True):
            self.processes = []
            started = time.monotonic()
            with self.subTest(cancelled=cancelled), patch.object(ir, "_launch_worker", side_effect=self.sleeping_worker):
                self.reject(reason="cancelled" if cancelled else "timeout",
                            limits=replace(ir.PdfIrLimits(), timeout_seconds=0.5),
                            cancel_check=(lambda: bool(self.processes)) if cancelled else None)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(len(self.processes), 1)
            self.assertIsNotNone(self.processes[0].poll())
            self.assertIsNone(sibling.poll())

    def run_cli(self, source, *extra):
        script = Path(__file__).resolve().parents[1] / "scripts" / "inspect-text-pdf-ir.py"
        return subprocess.run([sys.executable, "-I", "-B", str(script), str(source), *map(str, extra)],
                              cwd=self.root, capture_output=True, text=True, timeout=15,
                              env={"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root), "LANG": "C"})

    def test_cli_stdout_defaults_to_statistics_never_text_title_base64_or_path(self):
        path = self.write("cli.pdf", ir_fixture(images=True, metadata=True))
        process = self.run_cli(path)
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = json.loads(process.stdout)
        self.assertFalse(summary["private_report_written"])
        self.assertEqual(summary["unique_image_resources"], 1)
        self.assertIs(summary["eligible_for_payment"], False)
        for private in (RAW_LINES[0], "Synthetic title", "Synthetic author", base64.b64encode(ENCODED_IMAGE).decode(), str(path)):
            self.assertNotIn(private, process.stdout + process.stderr)
        self.assertNotIn("pages", summary)
        self.assertNotIn("resources", summary)
        self.assertNotIn("metadata", summary)

    def test_cli_explicit_private_report_contains_raw_data_with_mode_0600_and_no_source_change(self):
        path = self.write("cli-image.pdf", ir_fixture(images=True))
        before = path.read_bytes()
        report_path = self.root / "private-ir.json"
        process = self.run_cli(path, "--report", report_path)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)["private_report_written"])
        report = json.loads(report_path.read_text())
        self.assertIn(RAW_LINES[0], report["pages"][0]["raw_text"])
        self.assertEqual(base64.b64decode(report["resources"][0]["encoded_stream_base64"]), ENCODED_IMAGE)
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(RAW_LINES[0], process.stdout + process.stderr)

    def test_cli_existing_report_and_invalid_source_fail_without_overwrite_or_text_leak(self):
        report = self.write("already-exists.json", b"private previous report")
        process = self.run_cli(self.path, "--report", report)
        self.assertEqual(process.returncode, 3)
        self.assertEqual(report.read_bytes(), b"private previous report")
        bad = self.write("bad.pdf", b"%PDF-1.7\n" + RAW_LINES[0].encode())
        target = self.root / "must-not-exist.json"
        process = self.run_cli(bad, "--report", target)
        self.assertEqual(process.returncode, 2)
        self.assertIs(json.loads(process.stdout)["eligible_for_payment"], False)
        self.assertFalse(target.exists())
        self.assertNotIn(RAW_LINES[0], process.stdout + process.stderr)
        self.assertNotIn(str(self.root), process.stdout + process.stderr)

    def test_cli_cancel_and_timeout_errors_never_print_partial_raw_data(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "inspect-text-pdf-ir.py"
        spec = importlib.util.spec_from_file_location("d57_private_cli_fixture", script)
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(module)
        for error, expected in ((KeyboardInterrupt(), 130), (ir.PdfIrError("timeout"), 2)):
            output, errors = StringIO(), StringIO()
            with self.subTest(expected=expected), patch.object(module, "extract_pdf_text_ir", side_effect=error), \
                    redirect_stdout(output), redirect_stderr(errors):
                code = module.main([str(self.path)])
            self.assertEqual(code, expected)
            self.assertNotIn(RAW_LINES[0], output.getvalue() + errors.getvalue())
            self.assertNotIn(str(self.path), output.getvalue() + errors.getvalue())

    def test_cli_partial_dump_failure_or_interrupt_leaves_no_target_or_temporary_report(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "inspect-text-pdf-ir.py"
        spec = importlib.util.spec_from_file_location("d57_atomic_cli_fixture", script)
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(module)
        good = ir.extract_pdf_text_ir(self.path)
        before_files = {p.name for p in self.root.iterdir()}
        before_source = self.path.read_bytes()
        for interrupted in (False, True):
            target = self.root / ("interrupt.json" if interrupted else "write-failure.json")
            def partial_then_fail(value, stream, **kwargs):
                stream.write('{"private_partial":"' + RAW_LINES[0])
                stream.flush()
                if interrupted:
                    raise KeyboardInterrupt()
                raise OSError("synthetic disk failure")
            output, errors = StringIO(), StringIO()
            with self.subTest(interrupted=interrupted), patch.object(module, "extract_pdf_text_ir", return_value=good), \
                    patch.object(module.json, "dump", side_effect=partial_then_fail), redirect_stdout(output), redirect_stderr(errors):
                code = module.main([str(self.path), "--report", str(target)])
            self.assertEqual(code, 130 if interrupted else 3)
            self.assertFalse(target.exists())
            self.assertEqual({p.name for p in self.root.iterdir()}, before_files)
            self.assertEqual(self.path.read_bytes(), before_source)
            self.assertNotIn(RAW_LINES[0], output.getvalue() + errors.getvalue())

    def test_cli_publication_race_does_not_overwrite_winning_existing_report(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "inspect-text-pdf-ir.py"
        spec = importlib.util.spec_from_file_location("d57_race_cli_fixture", script)
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(module)
        good = ir.extract_pdf_text_ir(self.path)
        target = self.root / "race-winner.json"
        winner = b"previous competing writer must survive unchanged"
        original_link = os.link
        def publish_competitor_then_link(source, destination, *args, **kwargs):
            Path(destination).write_bytes(winner)
            return original_link(source, destination, *args, **kwargs)
        output, errors = StringIO(), StringIO()
        with patch.object(module, "extract_pdf_text_ir", return_value=good), \
                patch.object(module.os, "link", side_effect=publish_competitor_then_link), redirect_stdout(output), redirect_stderr(errors):
            code = module.main([str(self.path), "--report", str(target)])
        self.assertEqual(code, 3)
        self.assertEqual(target.read_bytes(), winner)
        self.assertEqual({p.name for p in self.root.iterdir()}, {self.path.name, target.name})
        self.assertNotIn(RAW_LINES[0], output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
