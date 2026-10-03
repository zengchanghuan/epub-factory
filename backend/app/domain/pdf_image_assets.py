"""Bounded, in-memory export of a deliberately small PDF image subset.

This is not a PDF renderer. Layout must independently account for every draw,
placement, clip and resource owner. Unsupported visual dependencies fail closed.
The caller supplies an already decrypted PdfReader inside its parser sandbox.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import warnings
import zlib

from pypdf.generic import ContentStream, DecodedStreamObject, IndirectObject, NullObject

_MESSAGES = {
    "invalid_limits": "PDF 图像导出限额无效。",
    "unsupported_image": "PDF 包含当前不能完整保留的图像或图像依赖。",
    "image_limit": "PDF 图像超过本次安全导出限额。",
    "invalid_image": "PDF 图像数据不完整或与声明不一致。",
    "decoder_unavailable": "PDF 图像解码依赖不可用。",
}
_LIMITS = (20_000_000, 100_000_000, 32 * 1024 * 1024, 64 * 1024 * 1024)
_FORBIDDEN = {"/Decode", "/DecodeParms", "/Mask", "/Matte", "/ImageMask",
              "/F", "/FFilter", "/FDecodeParms", "/Alternates", "/OPI",
              "/OC", "/Intent", "/SMaskInData", "/Ref"}


class PdfImageError(RuntimeError):
    """Public errors contain a fixed code/message, never source data or paths."""
    def __init__(self, reason):
        self.reason = reason if reason in _MESSAGES else "invalid_image"
        super().__init__(_MESSAGES[self.reason])


def _need(condition, reason="unsupported_image"):
    if not condition:
        raise PdfImageError(reason)


def _resolve(value):
    value = value.get_object() if hasattr(value, "get_object") else value
    return None if isinstance(value, NullObject) else value


def _mapping(value):
    value = _resolve(value)
    _need(isinstance(value, dict))
    return value


def _filters(stream):
    value = _resolve(stream.get("/Filter"))
    if isinstance(value, str):
        return [str(value)]
    _need(isinstance(value, list) and all(isinstance(x, str) for x in value))
    return list(map(str, value))


def _inflate(encoded, limit, *, exact=False):
    # Do not call pypdf.get_data(): its Flate allocation need not obey our budget.
    decoder = zlib.decompressobj()
    data = decoder.decompress(encoded, limit + 1)
    _need(len(data) <= limit, "image_limit")
    _need(not decoder.unconsumed_tail, "image_limit")
    _need(decoder.eof and not decoder.unused_data, "invalid_image")
    _need(not exact or len(data) == limit, "invalid_image")
    return data


def _check_page(page, content_budget):
    resources = _mapping(page.get("/Resources", {}))
    _need(not _resolve(page.get("/Group")))
    _need(not _resolve(resources.get("/Pattern")))
    colors = _mapping(resources.get("/ColorSpace", {}))
    # PDF permits resource defaults to reinterpret otherwise ordinary DeviceRGB
    # and DeviceGray images. Raw channel equality cannot prove color fidelity.
    _need(not ({"/DefaultRGB", "/DefaultGray"} & set(colors)))
    fonts = _mapping(resources.get("/Font", {}))
    for reference in fonts.values():
        font = _mapping(reference)
        _need(font.get("/Subtype") != "/Type3" and "/CharProcs" not in font)
    for reference in _mapping(resources.get("/ExtGState", {})).values():
        state = _mapping(reference)
        mask = _resolve(state.get("/SMask"))
        _need(mask is None or mask == "/None")
        # These states can reinterpret image colors/transparency. They are not
        # reproduced by copying image pixels into HTML.
        _need(not any(key in state for key in ("/TR", "/TR2", "/HT", "/BG", "/BG2", "/UCR", "/UCR2")))
        for key in ("/ca", "/CA"):
            _need(key not in state or state[key] == 1)
        _need("/BM" not in state or state["/BM"] == "/Normal")
        font = _resolve(state.get("/Font"))
        if font is not None:
            _need(isinstance(font, list) and len(font) == 2)
            font = _mapping(font[0])
            _need(font.get("/Subtype") != "/Type3" and "/CharProcs" not in font)
    annotations = _resolve(page.get("/Annots", []))
    _need(isinstance(annotations, list))
    for reference in annotations:
        _need(not _resolve(_mapping(reference).get("/AP")))

    # Parse only bounded page content, so an inline image cannot disappear from
    # an apparently empty XObject inventory. Complex stream filters fail closed.
    contents = _resolve(page.get("/Contents"))
    if contents is not None:
        streams = contents if isinstance(contents, list) else [contents]
        blocks, page_bytes = [], 0
        for reference in streams:
            stream = _mapping(reference)
            _need(not any(key in stream for key in ("/F", "/FFilter", "/FDecodeParms", "/DecodeParms")))
            encoded = getattr(stream, "_data", None)
            _need(isinstance(encoded, bytes), "invalid_image")
            _need(len(encoded) <= 8 * 1024 * 1024, "image_limit")
            remaining = min(8 * 1024 * 1024 - page_bytes, content_budget[0])
            _need(remaining > 0, "image_limit")
            if "/Filter" in stream:
                _need(_filters(stream) == ["/FlateDecode"])
                data = _inflate(encoded, remaining)
            else:
                _need(len(encoded) <= remaining, "image_limit")
                data = encoded
            page_bytes += len(data) + 1
            content_budget[0] -= len(data) + 1
            _need(page_bytes <= 8 * 1024 * 1024 and content_budget[0] >= 0, "image_limit")
            blocks.append(data)
        decoded = DecodedStreamObject()
        decoded.set_data(b"\n".join(blocks))
        content = ContentStream(decoded, None)
        _need(not any(operator == b"INLINE IMAGE" for _, operator in content.operations))
    return _mapping(resources.get("/XObject", {}))


def _descriptor(reference, *, mask=False):
    _need(isinstance(reference, IndirectObject))
    key = (int(reference.idnum), int(reference.generation))
    _need(key[0] > 0 and key[1] >= 0, "invalid_image")
    stream = _mapping(reference)
    _need(stream.get("/Subtype") == "/Image" and not (_FORBIDDEN & set(stream)))
    _need("/Interpolate" not in stream or not bool(getattr(stream["/Interpolate"], "value", stream["/Interpolate"])))
    _need(stream.get("/BitsPerComponent") == 8)
    _need(not mask or "/SMask" not in stream)
    color = stream.get("/ColorSpace")
    _need(color in ("/DeviceRGB", "/DeviceGray") and (not mask or color == "/DeviceGray"))
    encoding = _filters(stream)
    _need(encoding in (["/DCTDecode"], ["/FlateDecode"]) and (not mask or encoding == ["/FlateDecode"]))
    width, height = stream.get("/Width"), stream.get("/Height")
    _need(isinstance(width, int) and not isinstance(width, bool) and width > 0 and
          isinstance(height, int) and not isinstance(height, bool) and height > 0, "invalid_image")
    encoded = getattr(stream, "_data", None)
    _need(isinstance(encoded, bytes) and bool(encoded), "invalid_image")
    return {"key": key, "stream": stream, "size": (int(width), int(height)),
            "mode": "RGB" if color == "/DeviceRGB" else "L",
            "filter": encoding[0], "encoded": encoded}


class _BoundedOutput(BytesIO):
    def __init__(self, maximum):
        super().__init__()
        self.maximum = maximum

    def write(self, data):
        _need(self.tell() + len(data) <= self.maximum, "image_limit")
        return super().write(data)


def _decode(spec, Image):
    size, mode = spec["size"], spec["mode"]
    if spec["filter"] == "/FlateDecode":
        samples = _inflate(spec["encoded"], size[0] * size[1] * (3 if mode == "RGB" else 1), exact=True)
        return Image.frombytes(mode, size, samples)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(spec["encoded"])) as decoded:
            # Header checks precede load/copy: spoofed PDF dimensions cannot
            # authorize decoding a larger JPEG or silently changing color mode.
            _need(decoded.format == "JPEG" and decoded.mode == mode and decoded.size == size, "invalid_image")
            _need(decoded.getexif().get(274) in (None, 1) and not decoded.info.get("icc_profile"))
            return decoded.copy()


def export_images(reader, *, max_pixels_per_image=20_000_000,
                  max_total_pixels=100_000_000, max_encoded_bytes=32 * 1024 * 1024,
                  max_output_bytes=64 * 1024 * 1024) -> dict:
    """Return assets keyed by ``(object id, generation)`` without writing files.

    Encoded and pixel budgets count unique source objects, including soft masks.
    ``pages`` means resource-owner pages (one based), not image draw counts.
    Output-pixel SHA hashes native RGB/L/RGBA/LA bytes; size/mode are explicit.
    Limits may be reduced, not raised beyond this decoder's engineering caps.
    """
    limits = (max_pixels_per_image, max_total_pixels, max_encoded_bytes, max_output_bytes)
    _need(all(type(value) is int and 0 < value <= cap for value, cap in zip(limits, _LIMITS)), "invalid_limits")
    try:
        from PIL import Image
    except ImportError:
        raise PdfImageError("decoder_unavailable") from None
    try:
        assets, specifications, unique = {}, {}, {}
        total_pixels = total_encoded = 0
        content_budget = [32 * 1024 * 1024]
        for number, page in enumerate(reader.pages, 1):
            for reference in _check_page(page, content_budget).values():
                spec = _descriptor(reference)
                key = spec["key"]
                if key in specifications:
                    if number not in specifications[key]["pages"]:
                        specifications[key]["pages"].append(number)
                    continue
                spec["pages"] = [number]
                mask = None
                if "/SMask" in spec["stream"]:
                    mask = _descriptor(spec["stream"].raw_get("/SMask"), mask=True)
                    _need(mask["size"] == spec["size"], "unsupported_image")
                spec["mask"] = mask
                for item in (spec, mask):
                    if item is None or item["key"] in unique:
                        continue
                    pixels = item["size"][0] * item["size"][1]
                    _need(pixels <= max_pixels_per_image, "image_limit")
                    total_pixels += pixels
                    total_encoded += len(item["encoded"])
                    _need(total_pixels <= max_total_pixels and total_encoded <= max_encoded_bytes, "image_limit")
                    unique[item["key"]] = item
                specifications[key] = spec

        total_output = 0
        for key, spec in specifications.items():
            if spec["filter"] == "/DCTDecode" and spec["mask"] is None:
                _need(total_output + len(spec["encoded"]) <= max_output_bytes, "image_limit")
            base = _decode(spec, Image)
            try:
                source_pixels = base.tobytes()
                alpha = None
                if spec["mask"] is not None:
                    with _decode(spec["mask"], Image) as mask:
                        alpha = mask.tobytes()
                        base.putalpha(mask)
                original_jpeg = spec["filter"] == "/DCTDecode" and alpha is None
                if original_jpeg:
                    data, extension, mime = spec["encoded"], "jpg", "image/jpeg"
                    _need(total_output + len(data) <= max_output_bytes, "image_limit")
                else:
                    with _BoundedOutput(max_output_bytes - total_output) as buffer:
                        base.save(buffer, format="PNG", optimize=False)
                        data = buffer.getvalue()
                    extension, mime = "png", "image/png"
                total_output += len(data)
                with Image.open(BytesIO(data)) as check:
                    _need(check.size == spec["size"] and check.mode == base.mode, "invalid_image")
                    check.load()
                    _need(check.convert(spec["mode"]).tobytes() == source_pixels, "invalid_image")
                    _need(alpha is None or check.getchannel("A").tobytes() == alpha, "invalid_image")
                    pixel_sha = hashlib.sha256(check.tobytes()).hexdigest()
                assets[key] = {
                    "filename": f"pdf-image-{key[0]:06d}-g{key[1]}.{extension}",
                    "mime": mime, "data": data, "pixel_size": list(spec["size"]),
                    "pixel_mode": base.mode,
                    "source_encoded_sha256": hashlib.sha256(spec["encoded"]).hexdigest(),
                    "output_sha256": hashlib.sha256(data).hexdigest(),
                    "output_pixel_sha256": pixel_sha,
                    "has_smask": alpha is not None,
                    "alpha_sha256": hashlib.sha256(alpha).hexdigest() if alpha is not None else None,
                    "pages": spec["pages"],
                }
            finally:
                base.close()
        return assets
    except PdfImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
        raise PdfImageError("image_limit") from None
    except Exception:
        raise PdfImageError("invalid_image") from None
