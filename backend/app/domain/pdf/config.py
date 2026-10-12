"""Server-only configuration. No credentials in public capabilities or job metadata."""
import os
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path


class PdfError(ValueError):
    """Only fixed, credential-free messages may cross the public task boundary."""


class PageReviewRequired(PdfError):
    """A received response needs review; other pages can proceed without retrying it."""

    def __init__(self, page, reason):
        self.page = page
        self.reason = reason
        super().__init__(f'PDF 第 {page} 页内容待核验（{reason}），已保存响应，不会自动重复付费')


def positive_int(name, default, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
        if not 0 < value <= maximum:
            raise ValueError()
        return value
    except ValueError:
        raise PdfError(f"PDF 服务配置无效：{name}") from None


def root():
    path = Path(os.environ.get("PDF_WORK_DIR") or Path(__file__).resolve().parents[3] / "uploads" / ".pdf-work")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def max_bytes():
    return positive_int("PDF_MAX_FILE_MB", 300, 1000) * 1024 * 1024


def service_mode(value):
    if value not in {'ocr', 'translate'}:
        raise PdfError('请选择仅 OCR 或 OCR＋翻译')
    return value


def quote(pages, quality="standard", model="deepseek-flash", *, service='translate'):
    service_mode(service)
    prefix = 'PDF_OCR' if service == 'ocr' else 'PDF_TRANSLATION'
    try:
        # OCR retail approved 2026-09-27; explicit invalid overrides still fail closed.
        rate = Decimal(os.environ.get(f"{prefix}_PRICE_PER_PAGE_CNY", "0.05" if service == 'ocr' else "0"))
        minimum = Decimal(os.environ.get(f"{prefix}_MIN_PRICE_CNY", "1.99" if service == 'ocr' else "3.99"))
        if not rate.is_finite() or not minimum.is_finite() or rate <= 0 or minimum <= 0:
            raise ValueError()
        if type(pages) is not int or pages <= 0:
            raise ValueError()
        multiplier = Decimal('1') if service == 'ocr' else {"standard": Decimal("1"), "high": Decimal("1.5"), "literary": Decimal("2")}.get(quality, Decimal("1"))
        if service == 'translate' and "pro" in model:
            multiplier *= Decimal("3.4")
        amount = max(minimum, rate * pages * multiplier).quantize(Decimal("0.01"), rounding=ROUND_UP)
        return {"price_cny": str(amount), "pricing_basis": "pdf_pages", "total_pages": pages,
                "per_page_cny": str(rate), "minimum_cny": str(minimum),
                "quality_multiplier": str(multiplier), "quote_version": 2,
                "service_mode": service, "includes_translation": service == 'translate',
                "includes_ocr": True, "frozen": True}
    except (InvalidOperation, ValueError):
        label = '仅 OCR' if service == 'ocr' else 'OCR＋翻译'
        raise PdfError(f"PDF {label}尚未配置独立价格，暂不可创建订单") from None


def flash_credentials():
    # Explicit OCR credential wins. Reuse existing key only for the official DeepSeek origin.
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    key = os.environ.get("PDF_DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key and base in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise PdfError("PDF OCR 第三方处理服务尚未配置，请联系客服处理")
    return key


def ensure_enabled(service='translate'):
    service_mode(service)
    flag = 'PDF_OCR_ENABLED' if service == 'ocr' else 'PDF_TRANSLATION_ENABLED'
    if os.environ.get(flag, "0").lower() not in {"1", "true", "yes"}:
        raise PdfError(f"PDF {'仅 OCR' if service == 'ocr' else 'OCR＋翻译'}正在准备中，暂未开放")
    quote(1, service=service)
    flash_credentials()  # Every scan must have an independent, bounded fallback.
    positive_int("PDF_OCR_MAX_REQUESTS_PER_JOB", 600, 5000)
    positive_int("PDF_OCR_CONCURRENCY", 2, 4)
    try:
        import pypdfium2  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        raise PdfError("PDF 页面渲染组件尚未安装") from None


def capabilities():
    services = {}
    for service in ('ocr', 'translate'):
        try:
            ensure_enabled(service)
            services[service] = {"enabled": True, "pricing": quote(1, service=service)}
        except PdfError:
            services[service] = {"enabled": False}
    if not any(item['enabled'] for item in services.values()):
        return {"enabled": False, "services": services}
    try:
        return {"enabled": True, "services": services, "max_file_mb": max_bytes() // 1024 // 1024,
                "max_pages": positive_int("PDF_MAX_PAGES", 1000, 3000),
                "output": "epub_text_only", "single_file_only": True,
                "preview": "local_only_no_paid_ocr"}
    except PdfError:
        return {"enabled": False}
