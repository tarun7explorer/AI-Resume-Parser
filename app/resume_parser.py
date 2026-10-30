"""Bounded PDF text/layout extraction with an explicit Tesseract fallback."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.config import Settings

MAX_EXTRACTED_CHARS = 200_000
MAX_LAYOUT_SPANS = 20_000
MIN_SELECTABLE_TEXT = 35
MAX_OCR_PIXELS = 10_000_000


class DocumentProcessingError(Exception):
    """Safe processing failure with a stable code for the job queue/report."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class PageText:
    page: int
    text: str
    source: str


@dataclass(frozen=True)
class TextSpan:
    page: int
    text: str
    font_size: float
    bbox: tuple[float, float, float, float]
    color: int
    page_width: float
    page_height: float


@dataclass(frozen=True)
class ParsedDocument:
    pages: tuple[PageText, ...]
    spans: tuple[TextSpan, ...]
    page_count: int
    used_ocr: bool
    text: str


def _safe_import_fitz() -> Any:
    try:
        import fitz
    except ImportError:
        raise DocumentProcessingError(
            "pdf_dependency_missing",
            "PDF processing is unavailable. Install the project dependencies, including PyMuPDF.",
        ) from None
    return fitz


def _is_valid_signature(content: bytes) -> bool:
    return content.startswith(b"%PDF-") and b"%%EOF" in content[-2048:]


def _span_data(page: Any, page_number: int, *, max_remaining: int) -> tuple[list[TextSpan], int]:
    spans: list[TextSpan] = []
    count = 0
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return spans, count
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = str(span.get("text", ""))
                if not text.strip():
                    continue
                if count >= max_remaining:
                    return spans, count
                bbox = span.get("bbox", (0, 0, 0, 0))
                try:
                    coords = tuple(float(value) for value in bbox)
                    size = float(span.get("size", 0))
                    color = int(span.get("color", 0))
                except (TypeError, ValueError):
                    continue
                if len(coords) != 4 or not all(math.isfinite(value) for value in (*coords, size)):
                    continue
                spans.append(TextSpan(
                    page_number, text[:1200], size, coords, color,
                    float(page.rect.width), float(page.rect.height),
                ))
                count += 1
    return spans, count


def _tesseract(pages: list[tuple[int, Any]], settings: Settings) -> dict[int, str]:
    fitz = _safe_import_fitz()
    try:
        import pytesseract
    except ImportError:
        raise DocumentProcessingError(
            "ocr_wrapper_missing",
            "This PDF appears scanned. Install pytesseract and Pillow, and install the Tesseract OCR executable.",
        ) from None
    try:
        from PIL import Image
    except ImportError:
        raise DocumentProcessingError(
            "ocr_image_dependency_missing",
            "This PDF appears scanned. Install Pillow to enable OCR image conversion.",
        ) from None

    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        raise DocumentProcessingError(
            "tesseract_executable_missing",
            "This PDF appears scanned, but Tesseract OCR was not found. Install Tesseract or configure TESSERACT_CMD.",
        ) from None

    extracted: dict[int, str] = {}
    for page_number, page in pages:
        rect = page.rect
        width_points, height_points = float(rect.width), float(rect.height)
        if width_points <= 0 or height_points <= 0:
            continue
        scale = min(150 / 72, 4000 / width_points, 4000 / height_points)
        scale = min(scale, math.sqrt(MAX_OCR_PIXELS / (width_points * height_points)))
        try:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            text = pytesseract.image_to_string(
                image,
                lang=settings.ocr_language,
                timeout=settings.ocr_timeout_seconds,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            if error_type == "TesseractNotFoundError":
                raise DocumentProcessingError(
                    "tesseract_executable_missing",
                    "Tesseract OCR was not found. Install it or configure TESSERACT_CMD.",
                ) from None
            if error_type == "TesseractTimeoutError":
                raise DocumentProcessingError(
                    "ocr_page_timeout",
                    "OCR exceeded its per-page time limit. Reduce the PDF size or provide a text-based PDF.",
                    retryable=True,
                ) from None
            raise DocumentProcessingError(
                "ocr_processing_failed",
                "OCR could not read a scanned page. Try a clearer PDF or paste the job details as text.",
            ) from None
        normalized = re.sub(r"\s+", " ", text).strip()
        if normalized:
            extracted[page_number] = normalized
    return extracted


def extract_pdf(content: bytes, settings: Settings, *, mime_type: str = "application/pdf") -> ParsedDocument:
    """Extract a bounded PDF, OCR only sparse scanned pages, and retain page evidence."""

    if mime_type not in {"application/pdf", "application/octet-stream"}:
        raise DocumentProcessingError("invalid_pdf_mime_type", "Upload a PDF document.")
    if not content:
        raise DocumentProcessingError("empty_pdf", "The uploaded PDF is empty.")
    if len(content) > settings.resume_max_bytes:
        raise DocumentProcessingError("pdf_too_large", "The PDF exceeds the configured file-size limit.")
    if not _is_valid_signature(content):
        raise DocumentProcessingError("invalid_pdf_signature", "The file is not a complete PDF document.")

    fitz = _safe_import_fitz()
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception:
        raise DocumentProcessingError("pdf_parse_failed", "The PDF is corrupt or malformed and could not be opened.") from None

    try:
        if document.is_encrypted:
            raise DocumentProcessingError("pdf_encrypted", "Password-protected PDFs are not supported.")
        page_count = len(document)
        if page_count < 1:
            raise DocumentProcessingError("pdf_empty_document", "The PDF contains no pages.")
        if page_count > settings.pdf_max_pages:
            raise DocumentProcessingError("pdf_page_limit", f"PDFs may contain at most {settings.pdf_max_pages} pages.")

        page_texts: dict[int, PageText] = {}
        sparse_pages: list[tuple[int, Any]] = []
        spans: list[TextSpan] = []
        total_chars = 0
        for index, page in enumerate(document):
            page_number = index + 1
            try:
                text = page.get_text("text")
            except Exception:
                raise DocumentProcessingError("pdf_page_read_failed", "A PDF page could not be read.") from None
            text = re.sub(r"[ \t]+", " ", text).strip()
            if text:
                total_chars += len(text)
                if total_chars > MAX_EXTRACTED_CHARS:
                    raise DocumentProcessingError("pdf_text_limit", "The PDF contains too much extracted text to process safely.")
                page_texts[page_number] = PageText(page_number, text, "text")
            page_spans, span_count = _span_data(page, page_number, max_remaining=MAX_LAYOUT_SPANS - len(spans))
            spans.extend(page_spans)
            image_page = False
            try:
                image_page = bool(page.get_images(full=True))
            except Exception:
                pass
            # A genuinely blank page is not automatically a scanned page. Only
            # render pages for OCR when PDF image objects provide scan evidence.
            if len(text) < MIN_SELECTABLE_TEXT and image_page:
                sparse_pages.append((page_number, page))

        used_ocr = False
        if sparse_pages:
            if len(sparse_pages) > settings.ocr_max_pages:
                raise DocumentProcessingError(
                    "ocr_page_limit",
                    f"This scanned PDF has more than the OCR limit of {settings.ocr_max_pages} pages.",
                )
            recognized = _tesseract(sparse_pages, settings)
            for page_number, text in recognized.items():
                if text:
                    old_text = page_texts.get(page_number)
                    if old_text is None or len(text) > len(old_text.text):
                        page_texts[page_number] = PageText(page_number, text, "ocr")
                        used_ocr = True
                    total_chars += len(text)
                    if total_chars > MAX_EXTRACTED_CHARS:
                        raise DocumentProcessingError("pdf_text_limit", "The PDF contains too much extracted text to process safely.")

        ordered_pages = tuple(page_texts[index] for index in sorted(page_texts))
        full_text = "\n\n".join(f"[page:{page.page}]\n{page.text}" for page in ordered_pages)
        if len(full_text.strip()) < 20:
            raise DocumentProcessingError(
                "pdf_no_readable_text",
                "No readable text was found. Try a clearer, selectable-text PDF or an OCR-ready scan.",
            )
        return ParsedDocument(ordered_pages, tuple(spans), page_count, used_ocr, full_text)
    finally:
        document.close()
