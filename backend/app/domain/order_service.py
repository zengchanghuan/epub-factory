"""Stable service names for payment receipts and order views."""


def order_kind(job):
    if (getattr(job, "translation_stats", {}) or {}).get("pdf_conversion") or getattr(getattr(job, "output_mode", ""), "value", "") == "original":
        return "pdf_conversion"
    if str(getattr(job, 'input_path', '')).lower().endswith('.pdf'):
        return 'pdf_translation' if job.enable_translation else 'pdf_ocr'
    return 'translation' if job.enable_translation else 'conversion'


SERVICE_LABELS = {'pdf_conversion': 'PDF 转 EPUB（保留原文）', 'pdf_ocr': 'PDF 仅 OCR', 'pdf_translation': 'PDF OCR＋翻译',
                  'translation': '电子书翻译', 'conversion': 'EPUB 转换'}
