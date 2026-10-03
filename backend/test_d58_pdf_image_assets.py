"""Synthetic B2 image export contracts; no manuscripts, files or model calls.

Actual pypdf 6.0.0 readers and Pillow decoders are used. PDF bytes remain in
memory. These checks establish pixel/alpha preservation, not page layout.
"""
from contextlib import ExitStack
import copy
import hashlib
from io import BytesIO
import socket
import unittest
from unittest.mock import patch
import zlib

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,
                           DictionaryObject, EncodedStreamObject, IndirectObject,
                           NameObject, NumberObject, TextStringObject)

from app.domain import pdf_image_assets as images


def sha(data):
    return hashlib.sha256(data).hexdigest()


def image_spec(mode="RGB", encoding="DCT", size=(3, 2), samples=None, **save_options):
    samples = samples if samples is not None else bytes(range(size[0] * size[1] * (3 if mode == "RGB" else 1)))
    if encoding == "DCT":
        with Image.frombytes(mode, size, samples) as image, BytesIO() as output:
            image.save(output, format="JPEG", quality=95, **save_options)
            encoded = output.getvalue()
    else:
        encoded = zlib.compress(samples)
    return {"size": size, "mode": mode, "encoding": encoding, "encoded": encoded}


def fixture(specs, *, pages=1, duplicate=False, inline=False, content_filter=False):
    writer = PdfWriter()

    def add(spec):
        stream = EncodedStreamObject()
        stream._data = spec["encoded"]
        stream.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                       NameObject("/Width"): NumberObject(spec["size"][0]),
                       NameObject("/Height"): NumberObject(spec["size"][1]),
                       NameObject("/BitsPerComponent"): NumberObject(8),
                       NameObject("/ColorSpace"): NameObject("/DeviceRGB" if spec["mode"] == "RGB" else "/DeviceGray"),
                       NameObject("/Filter"): NameObject("/DCTDecode" if spec["encoding"] == "DCT" else "/FlateDecode")})
        if spec.get("mask"):
            stream[NameObject("/SMask")] = add(spec["mask"])
        stream.update(spec.get("overrides", {}))
        return writer._add_object(stream)

    refs = [add(spec) for spec in specs]
    for _ in range(pages):
        page = writer.add_blank_page(width=100, height=100)
        xobjects = DictionaryObject({NameObject(f"/I{i}"): ref for i, ref in enumerate(refs)})
        if duplicate and refs:
            xobjects[NameObject("/Again")] = refs[0]
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): xobjects})
        content = b"q " + b" ".join(f"/I{i} Do".encode() for i in range(len(refs))) + b" Q"
        if inline:
            content += b" q BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00 EI Q"
        if content_filter:
            body = EncodedStreamObject()
            body._data = zlib.compress(content)
            body[NameObject("/Filter")] = NameObject("/FlateDecode")
        else:
            body = DecodedStreamObject()
            body.set_data(content)
        page[NameObject("/Contents")] = writer._add_object(body)
    output = BytesIO()
    writer.write(output)
    return PdfReader(BytesIO(output.getvalue()), strict=True)


def resource(reader, name="/I0", page=0):
    return reader.pages[page]["/Resources"]["/XObject"][name]


class PdfImageAssetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.guards = [self.stack.enter_context(patch.object(socket.socket, key, side_effect=AssertionError("network forbidden")))
                       for key in ("connect", "connect_ex", "sendto", "sendmsg")]
        self.guards.append(self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))))
        self.addCleanup(lambda: [guard.assert_not_called() for guard in self.guards])

    def reject(self, reader, reason, **kwargs):
        with self.assertRaises(images.PdfImageError) as caught:
            images.export_images(reader, **kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), images._MESSAGES[reason])
        self.assertNotIn("PRIVATE_SOURCE", str(caught.exception))

    def assert_pixels(self, result, expected_mode, expected_pixels, size):
        self.assertEqual(result["pixel_mode"], expected_mode)
        self.assertEqual(result["pixel_size"], list(size))
        self.assertEqual(result["output_sha256"], sha(result["data"]))
        self.assertEqual(result["output_pixel_sha256"], sha(expected_pixels))
        with Image.open(BytesIO(result["data"])) as decoded:
            self.assertEqual(decoded.mode, expected_mode)
            self.assertEqual(decoded.size, size)
            self.assertEqual(decoded.tobytes(), expected_pixels)

    def test_rgb_and_gray_jpeg_keep_exact_encoding_and_decoded_pixels(self):
        for mode in ("RGB", "L"):
            with self.subTest(mode=mode):
                spec = image_spec(mode)
                reader = fixture([spec])
                before = bytes(resource(reader)._data)
                result, = images.export_images(reader).values()
                self.assertEqual(result["data"], spec["encoded"])
                self.assertEqual(result["source_encoded_sha256"], sha(spec["encoded"]))
                self.assertEqual(result["mime"], "image/jpeg")
                self.assertFalse(result["has_smask"])
                self.assertIsNone(result["alpha_sha256"])
                with Image.open(BytesIO(spec["encoded"])) as original:
                    self.assert_pixels(result, mode, original.tobytes(), spec["size"])
                self.assertEqual(resource(reader)._data, before)

    def test_rgb_and_gray_flate_become_lossless_png(self):
        for mode in ("RGB", "L"):
            with self.subTest(mode=mode):
                spec = image_spec(mode, "Flate")
                result, = images.export_images(fixture([spec], content_filter=True)).values()
                self.assertEqual(result["mime"], "image/png")
                self.assert_pixels(result, mode, zlib.decompress(spec["encoded"]), spec["size"])

    def test_jpeg_soft_mask_retains_all_rgb_including_transparent_pixels(self):
        spec = image_spec()
        alpha = bytes([0, 19, 255, 128, 33, 250])
        spec["mask"] = image_spec("L", "Flate", samples=alpha)
        result, = images.export_images(fixture([spec])).values()
        with Image.open(BytesIO(spec["encoded"])) as original:
            original.putalpha(Image.frombytes("L", spec["size"], alpha))
            self.assert_pixels(result, "RGBA", original.tobytes(), spec["size"])
        self.assertTrue(result["has_smask"])
        self.assertEqual(result["alpha_sha256"], sha(alpha))
        self.assertEqual(result["mime"], "image/png")

    def test_gray_soft_mask_is_la_not_rgb_conversion(self):
        spec = image_spec("L", "Flate")
        alpha = b"\x00\x44\x88\xaa\xcc\xff"
        spec["mask"] = image_spec("L", "Flate", samples=alpha)
        result, = images.export_images(fixture([spec])).values()
        pixels = b"".join(bytes([gray, a]) for gray, a in zip(zlib.decompress(spec["encoded"]), alpha))
        self.assert_pixels(result, "LA", pixels, spec["size"])

    def test_dedup_identity_counts_pages_not_draws(self):
        spec = image_spec()
        reader = fixture([spec], pages=3, duplicate=True)
        assets = images.export_images(reader, max_total_pixels=6, max_encoded_bytes=len(spec["encoded"]))
        self.assertEqual(len(assets), 1)
        key, = assets
        self.assertIsInstance(key, tuple)
        self.assertEqual(assets[key]["pages"], [1, 2, 3])
        self.assertIn(f"{key[0]:06d}-g{key[1]}", assets[key]["filename"])

    def test_generation_is_part_of_identity_not_only_object_number(self):
        reader = fixture([image_spec()])
        refs = reader.pages[0]["/Resources"]["/XObject"]
        original = refs.raw_get("/I0")
        clone = copy.deepcopy(original.get_object())
        reader.resolved_objects[(1, original.idnum)] = clone
        refs[NameObject("/GenerationOne")] = IndirectObject(original.idnum, 1, reader)
        assets = images.export_images(reader)
        self.assertEqual(set(assets), {(original.idnum, 0), (original.idnum, 1)})
        self.assertEqual(len({x["filename"] for x in assets.values()}), 2)

    def test_empty_image_inventory_is_explicitly_empty(self):
        self.assertEqual(images.export_images(fixture([])), {})

    def test_unknown_image_interpretations_are_refused_before_decoding(self):
        cases = {"/ColorSpace": NameObject("/DeviceCMYK"), "/Decode": ArrayObject([NumberObject(1), NumberObject(0)]),
                 "/DecodeParms": DictionaryObject(), "/Matte": ArrayObject(), "/Mask": ArrayObject(),
                 "/ImageMask": BooleanObject(False), "/F": TextStringObject("PRIVATE_SOURCE_REMOTE"),
                 "/FFilter": NameObject("/FlateDecode"), "/FDecodeParms": DictionaryObject(),
                 "/Alternates": ArrayObject(), "/Intent": NameObject("/Perceptual"),
                 "/SMaskInData": NumberObject(1), "/Interpolate": BooleanObject(True),
                 "/BitsPerComponent": NumberObject(16), "/Filter": NameObject("/JPXDecode")}
        for key, value in cases.items():
            with self.subTest(key=key), patch.object(images, "_decode") as decoder:
                reader = fixture([image_spec()])
                resource(reader)[NameObject(key)] = value
                self.reject(reader, "unsupported_image")
                decoder.assert_not_called()

    def test_filter_chain_and_direct_main_object_refused(self):
        reader = fixture([image_spec()])
        resource(reader)[NameObject("/Filter")] = ArrayObject([NameObject("/ASCII85Decode"), NameObject("/DCTDecode")])
        self.reject(reader, "unsupported_image")
        reader = fixture([image_spec()])
        refs = reader.pages[0]["/Resources"]["/XObject"]
        refs[NameObject("/I0")] = resource(reader)
        self.reject(reader, "unsupported_image")

    def test_invalid_mask_interpretations_and_dimensions_refused(self):
        masks = [image_spec("RGB", "Flate"), image_spec("L", "DCT"), image_spec("L", "Flate", (2, 2))]
        for mask in masks:
            with self.subTest(mask=mask["mode"] + mask["encoding"] + str(mask["size"])):
                spec = image_spec();spec["mask"] = mask
                self.reject(fixture([spec]), "unsupported_image")
        spec = image_spec();spec["mask"] = image_spec("L", "Flate")
        for key in ("/Decode", "/Matte", "/SMask"):
            reader = fixture([spec])
            resource(reader)["/SMask"][NameObject(key)] = ArrayObject()
            self.reject(reader, "unsupported_image")

    def test_inline_images_and_forms_are_not_silently_omitted(self):
        self.reject(fixture([], inline=True), "unsupported_image")
        reader = fixture([image_spec()])
        resource(reader)[NameObject("/Subtype")] = NameObject("/Form")
        self.reject(reader, "unsupported_image")

    def test_alternate_image_carriers_are_rejected(self):
        # These fixtures contain actual image streams in the alternative carrier.
        from test_d57_pdf_text_ir import alternate_image_carrier_fixture
        for carrier in ("pattern", "annotation_appearance", "type3_charproc", "soft_mask_group"):
            with self.subTest(carrier=carrier):
                reader = PdfReader(BytesIO(alternate_image_carrier_fixture(carrier)), strict=True)
                self.reject(reader, "unsupported_image")

    def test_nonopaque_or_color_transform_graphics_state_refused(self):
        for key, value in (("/ca", NumberObject(0)), ("/BM", NameObject("/Multiply")),
                           ("/TR", TextStringObject("PRIVATE_SOURCE_TRANSFORM"))):
            reader = fixture([image_spec()])
            reader.pages[0]["/Resources"][NameObject("/ExtGState")] = DictionaryObject({
                NameObject("/G0"): DictionaryObject({NameObject(key): value})})
            self.reject(reader, "unsupported_image")

    def test_device_color_defaults_and_page_transparency_group_refused(self):
        for key in ("/DefaultRGB", "/DefaultGray"):
            reader = fixture([image_spec()])
            reader.pages[0]["/Resources"][NameObject("/ColorSpace")] = DictionaryObject({
                NameObject(key): NameObject("/PRIVATE_SOURCE_CUSTOM_SPACE")})
            with patch.object(images, "_decode") as decoder:
                self.reject(reader, "unsupported_image")
                decoder.assert_not_called()
        reader = fixture([image_spec()])
        reader.pages[0][NameObject("/Group")] = DictionaryObject({NameObject("/S"): NameObject("/Transparency")})
        self.reject(reader, "unsupported_image")

    def test_all_dimension_and_encoded_budgets_checked_before_any_decode(self):
        one, two = image_spec(), image_spec()
        for kwargs in ({"max_pixels_per_image": 5}, {"max_total_pixels": 11},
                       {"max_encoded_bytes": len(one["encoded"]) * 2 - 1}):
            with self.subTest(kwargs=kwargs), patch.object(images, "_decode") as decoder:
                self.reject(fixture([one, two]), "image_limit", **kwargs)
                decoder.assert_not_called()
        reader = fixture([one, two])
        resource(reader, "/I1")[NameObject("/Width")] = NumberObject(10**10)
        with patch.object(images, "_decode") as decoder:
            self.reject(reader, "image_limit")
            decoder.assert_not_called()

    def test_mask_counts_toward_both_pixel_and_encoded_budgets(self):
        spec = image_spec();spec["mask"] = image_spec("L", "Flate")
        for kwargs in ({"max_total_pixels": 11}, {"max_encoded_bytes": len(spec["encoded"]) + len(spec["mask"]["encoded"]) - 1}):
            with patch.object(images, "_decode") as decoder:
                self.reject(fixture([spec]), "image_limit", **kwargs)
                decoder.assert_not_called()

    def test_flate_bomb_truncated_and_trailing_stream_rejected(self):
        spec = image_spec("L", "Flate", (1, 1))
        for encoded, reason in ((zlib.compress(b"x" * 1_000_000), "image_limit"),
                                (spec["encoded"][:-1], "invalid_image"),
                                (spec["encoded"] + b"PRIVATE_SOURCE_TRAILING", "invalid_image"),
                                (zlib.compress(b""), "invalid_image")):
            with self.subTest(reason=reason):
                self.reject(fixture([{**spec, "encoded": encoded}]), reason)

    def test_jpeg_header_cannot_override_pdf_size_or_color(self):
        reader = fixture([image_spec()])
        resource(reader)[NameObject("/Width")] = NumberObject(2)
        self.reject(reader, "invalid_image")
        reader = fixture([image_spec()])
        resource(reader)[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
        self.reject(reader, "invalid_image")
        self.reject(fixture([{**image_spec(), "encoded": b"PRIVATE_SOURCE_NOT_JPEG"}]), "invalid_image")

    def test_pillow_bomb_warning_and_error_fail_closed(self):
        for threshold in (4, 1):
            with self.subTest(threshold=threshold), patch.object(Image, "MAX_IMAGE_PIXELS", threshold):
                self.reject(fixture([image_spec()]), "image_limit")

    def test_exif_rotation_and_icc_profile_are_not_silently_lost(self):
        exif = Image.Exif();exif[274] = 6
        for spec in (image_spec(exif=exif), image_spec(icc_profile=b"PRIVATE_SOURCE_ICC")):
            self.reject(fixture([spec]), "unsupported_image")

    def test_total_output_budget_applies_to_jpeg_and_png(self):
        for specs in ([image_spec()], [image_spec("RGB", "Flate")],
                      [image_spec("RGB", "Flate"), image_spec("L", "Flate")]):
            assets = images.export_images(fixture(specs))
            size = sum(len(x["data"]) for x in assets.values())
            self.reject(fixture(specs), "image_limit", max_output_bytes=size - 1)
        spec = image_spec()
        with patch.object(images, "_decode") as decoder:
            self.reject(fixture([spec]), "image_limit", max_output_bytes=len(spec["encoded"]) - 1)
            decoder.assert_not_called()

    def test_invalid_limits_are_not_coerced_or_unbounded(self):
        for name in ("max_pixels_per_image", "max_total_pixels", "max_encoded_bytes", "max_output_bytes"):
            for value in (0, -1, True, 1.5, "8", 10**12):
                with self.subTest(name=name, value=value):
                    self.reject(fixture([]), "invalid_limits", **{name: value})

    def test_repeat_export_is_stable_and_does_not_mutate_source_streams(self):
        spec = image_spec();spec["mask"] = image_spec("L", "Flate")
        reader = fixture([spec])
        before = (resource(reader)._data, resource(reader)["/SMask"]._data, tuple(resource(reader)))
        first, second = images.export_images(reader), images.export_images(reader)
        self.assertEqual(first, second)
        self.assertEqual(before, (resource(reader)._data, resource(reader)["/SMask"]._data, tuple(resource(reader))))


if __name__ == "__main__":
    unittest.main()
