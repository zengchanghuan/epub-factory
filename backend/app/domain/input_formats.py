"""Public input policy; PDF adapter remains deferred, not a supported product."""

SUPPORTED_EXTENSIONS = ('.epub', '.mobi', '.azw3', '.docx', '.md', '.markdown')
PDF_DISABLED_MESSAGE = '暂不支持 PDF，请先转换为 EPUB 后上传。'
UNSUPPORTED_MESSAGE = '仅支持 .epub, .mobi, .azw3, .docx 或 .md 文件'


def validate_filename(filename: str | None) -> None:
    lower = (filename or '').lower()
    if lower.endswith('.pdf'):
        raise ValueError(PDF_DISABLED_MESSAGE)
    if not any(lower.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
        raise ValueError(UNSUPPORTED_MESSAGE)


def is_pdf_header(prefix: bytes) -> bool:
    return prefix.lstrip(b'\xef\xbb\xbf\r\n\t ').startswith(b'%PDF-')
