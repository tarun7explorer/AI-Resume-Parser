"""PDF validation and page-level text extraction (app/resume_parser.py).

Responsibilities
----------------
* Validate PDF bytes: non-empty, ``%PDF-`` signature, byte limit, page limit,
  encryption, malformed structure, text-size limit, wall-clock limit.
* Extract text page by page with PyMuPDF and keep page-level provenance.
* Expose a ``ParsedDocument`` whose ``full_text`` is the single source of truth
  for evidence verification. Every page owns a half-open offset range
  ``[start_offset, end_offset)`` inside ``full_text``; pages are joined with a
  single form-feed separator (``"\\f"``) that belongs to no page.
* Identify image-only (scanned) pages. **OCR is NOT implemented.** Scanned or
  image-only PDFs are reported as requiring OCR / manual review. No text is
  ever invented for them.
* Write temporary files only under a private, randomly named directory and
  always remove them (success, failure, interpreter exit).

Resume contents are untrusted data. Nothing here interprets, executes or
follows instructions found inside a PDF.

Configuration (environment variables)
-------------------------------------
Existing names from Overview.txt:
    MAX_UPLOAD_BYTES   (default 10485760)
    MAX_PDF_PAGES      (default 30)
New names introduced by this module (add to .env.example):
    MAX_PDF_TEXT_CHARS          (default 300000)
    PDF_PARSE_TIMEOUT_SECONDS   (default 20)
    MIN_EXTRACTED_CHARS         (default 50)
"""

from __future__ import annotations

import atexit
import bisect
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Iterator, Mapping, Sequence

try:  # PyMuPDF >= 1.24 exposes ``pymupdf``; older releases expose ``fitz``.
    import pymupdf
except ImportError:  # pragma: no cover - depends on installed version
    import fitz as pymupdf  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

try:  # keep MuPDF diagnostics out of stderr; we raise our own safe errors
    pymupdf.TOOLS.mupdf_display_errors(False)
except Exception:  # pragma: no cover - defensive
    pass

__all__ = [
    "PAGE_SEPARATOR",
    "EmptyPDFError",
    "EncryptedPDFError",
    "InvalidPDFSignatureError",
    "MalformedPDFError",
    "NoExtractableTextError",
    "OCRRequiredError",
    "PDFPageLimitError",
    "PDFProcessingLimitError",
    "PDFTextTooLargeError",
    "PDFTooLargeError",
    "PDFValidationError",
    "PageText",
    "ParsedDocument",
    "ParserLimits",
    "ResumeParser",
    "active_temp_file_count",
    "cleanup_temp_files",
    "parse_pdf_bytes",
    "secure_temp_pdf",
    "sweep_stale_temp_files",
]

PAGE_SEPARATOR = "\f"
_SIGNATURE = b"%PDF-"
_SIGNATURE_WINDOW = 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# --------------------------------------------------------------------------- #
# Exceptions (messages are safe to return to clients; they never contain
# document content)
# --------------------------------------------------------------------------- #
class PDFValidationError(Exception):
    """Base class for all PDF validation/extraction failures."""

    code: str = "pdf_invalid"
    http_status: int = 422
    default_message: str = "The PDF could not be processed."

    def __init__(self, message: str | None = None, *, details: Mapping[str, object] | None = None):
        self.public_message = message or self.default_message
        self.details: dict[str, object] = dict(details or {})
        super().__init__(self.public_message)

    def to_dict(self) -> dict[str, object]:
        """Sanitized error payload."""
        payload: dict[str, object] = {"code": self.code, "message": self.public_message}
        if self.details:
            payload["details"] = self.details
        return payload


class EmptyPDFError(PDFValidationError):
    code = "pdf_empty_file"
    http_status = 400
    default_message = "The uploaded file is empty."


class InvalidPDFSignatureError(PDFValidationError):
    code = "pdf_invalid_signature"
    http_status = 400
    default_message = "The uploaded file is not a PDF (missing %PDF- signature)."


class PDFTooLargeError(PDFValidationError):
    code = "pdf_too_large"
    http_status = 413
    default_message = "The PDF exceeds the maximum allowed file size."


class PDFPageLimitError(PDFValidationError):
    code = "pdf_too_many_pages"
    default_message = "The PDF exceeds the maximum allowed page count."


class EncryptedPDFError(PDFValidationError):
    code = "pdf_encrypted"
    default_message = "The PDF is password-protected. Upload an unprotected copy."


class MalformedPDFError(PDFValidationError):
    code = "pdf_malformed"
    default_message = "The PDF is malformed or corrupted and could not be read."


class NoExtractableTextError(PDFValidationError):
    code = "pdf_no_extractable_text"
    default_message = (
        "No extractable text was found in the PDF. It may be blank or use outlined "
        "graphics for text. Manual review is required."
    )


class OCRRequiredError(NoExtractableTextError):
    code = "pdf_requires_ocr"
    default_message = (
        "The PDF appears to be scanned or image-only. OCR is not supported; "
        "manual review or a text-based PDF is required."
    )


class PDFProcessingLimitError(PDFValidationError):
    code = "pdf_processing_limit"
    default_message = "The PDF took too long to process."


class PDFTextTooLargeError(PDFValidationError):
    code = "pdf_text_too_large"
    default_message = "The PDF contains more text than the configured limit."


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_number(name: str, default: float, *, minimum: float, integer: bool) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a valid {'integer' if integer else 'number'}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class ParserLimits:
    """Resource limits applied to every PDF."""

    max_bytes: int = 10_485_760
    max_pages: int = 30
    max_text_chars: int = 300_000
    timeout_seconds: float = 20.0
    min_extracted_chars: int = 50

    def __post_init__(self) -> None:
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.max_text_chars < 1:
            raise ValueError("max_text_chars must be >= 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.min_extracted_chars < 0:
            raise ValueError("min_extracted_chars must be >= 0")

    @classmethod
    def from_env(cls) -> "ParserLimits":
        """Build limits from environment variables; raises ``ValueError`` if invalid."""
        return cls(
            max_bytes=int(_env_number("MAX_UPLOAD_BYTES", 10_485_760, minimum=1, integer=True)),
            max_pages=int(_env_number("MAX_PDF_PAGES", 30, minimum=1, integer=True)),
            max_text_chars=int(_env_number("MAX_PDF_TEXT_CHARS", 300_000, minimum=1, integer=True)),
            timeout_seconds=float(
                _env_number("PDF_PARSE_TIMEOUT_SECONDS", 20.0, minimum=0.001, integer=False)
            ),
            min_extracted_chars=int(_env_number("MIN_EXTRACTED_CHARS", 50, minimum=0, integer=True)),
        )


# --------------------------------------------------------------------------- #
# Parsed document model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PageText:
    """Text of one page (1-based ``page_number``) and its place in ``full_text``."""

    page_number: int
    text: str
    start_offset: int
    end_offset: int  # exclusive

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class ParsedDocument:
    """Extracted text with page provenance.

    ``full_text`` is the canonical text: ``PAGE_SEPARATOR.join(page texts)``.
    The page text is the PDF text layer as extracted (control characters removed,
    line endings normalised to ``\\n``) and is never rewritten afterwards.
    """

    sha256: str
    byte_size: int
    page_count: int
    pages: tuple[PageText, ...]
    full_text: str
    image_only_pages: tuple[int, ...] = ()
    empty_pages: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()
    is_encrypted: bool = False
    was_repaired: bool = False

    @classmethod
    def from_page_texts(
        cls,
        page_texts: Sequence[str],
        *,
        sha256: str = "",
        byte_size: int = 0,
        image_only_pages: Sequence[int] = (),
        warnings: Sequence[str] = (),
        is_encrypted: bool = False,
        was_repaired: bool = False,
    ) -> "ParsedDocument":
        """Build a document (and consistent offsets) from per-page strings."""
        pages: list[PageText] = []
        empty: list[int] = []
        cursor = 0
        for number, text in enumerate(page_texts, start=1):
            if PAGE_SEPARATOR in text:
                raise ValueError("page text must not contain the page separator character")
            end = cursor + len(text)
            pages.append(PageText(number, text, cursor, end))
            if not text.strip():
                empty.append(number)
            cursor = end + len(PAGE_SEPARATOR)
        return cls(
            sha256=sha256,
            byte_size=byte_size,
            page_count=len(pages),
            pages=tuple(pages),
            full_text=PAGE_SEPARATOR.join(page_texts),
            image_only_pages=tuple(image_only_pages),
            empty_pages=tuple(empty),
            warnings=tuple(warnings),
            is_encrypted=is_encrypted,
            was_repaired=was_repaired,
        )

    # -- provenance helpers -------------------------------------------------- #
    @cached_property
    def _page_starts(self) -> tuple[int, ...]:
        return tuple(page.start_offset for page in self.pages)

    def page_for_offset(self, offset: int) -> int:
        """Return the 1-based page containing ``offset`` of ``full_text``.

        An offset that lands on a page separator maps to the preceding page.
        """
        if not self.pages or offset < 0 or offset >= len(self.full_text):
            raise ValueError("offset outside document text")
        return self.pages[bisect.bisect_right(self._page_starts, offset) - 1].page_number

    def pages_for_span(self, start: int, end: int) -> tuple[int, int]:
        """Return (first_page, last_page) covered by the half-open span ``[start, end)``."""
        if end <= start:
            raise ValueError("span must be non-empty")
        return self.page_for_offset(start), self.page_for_offset(end - 1)

    # -- quality indicators -------------------------------------------------- #
    @property
    def char_count(self) -> int:
        return len(self.full_text)

    @property
    def extraction_complete(self) -> bool:
        """False when at least one page was image-only (text could not be extracted)."""
        return not self.image_only_pages

    @property
    def needs_manual_review(self) -> bool:
        return bool(self.image_only_pages) or self.was_repaired


# --------------------------------------------------------------------------- #
# Safe temporary files
# --------------------------------------------------------------------------- #
_TEMP_LOCK = threading.Lock()
_TEMP_DIR: Path | None = None
_ACTIVE_TEMP_FILES: set[str] = set()


def _get_temp_dir() -> Path:
    """Private (mode 0700), randomly named directory created on first use."""
    global _TEMP_DIR
    with _TEMP_LOCK:
        if _TEMP_DIR is None or not _TEMP_DIR.is_dir():
            _TEMP_DIR = Path(tempfile.mkdtemp(prefix="resume-parser-"))
        return _TEMP_DIR


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("temp_file_remove_failed")


@contextmanager
def secure_temp_pdf(data: bytes) -> Iterator[str]:
    """Write ``data`` to a randomly named temp file and always delete it.

    File names are generated by ``tempfile.mkstemp`` (mode 0600); no caller-
    supplied name is ever used, so path traversal is impossible.
    """
    directory = _get_temp_dir()
    fd, name = tempfile.mkstemp(prefix="pdf-", suffix=".pdf", dir=directory)
    with _TEMP_LOCK:
        _ACTIVE_TEMP_FILES.add(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        yield name
    finally:
        _remove_quietly(name)
        with _TEMP_LOCK:
            _ACTIVE_TEMP_FILES.discard(name)


def active_temp_file_count() -> int:
    """Number of temp files currently registered (0 when idle)."""
    with _TEMP_LOCK:
        return len(_ACTIVE_TEMP_FILES)


def cleanup_temp_files() -> None:
    """Remove every temp file and the temp directory. Call on worker shutdown.

    Registered with ``atexit``; the worker's SIGTERM handler should call it too.
    Do not call while a parse is in flight.
    """
    global _TEMP_DIR
    with _TEMP_LOCK:
        for name in list(_ACTIVE_TEMP_FILES):
            _remove_quietly(name)
        _ACTIVE_TEMP_FILES.clear()
        directory, _TEMP_DIR = _TEMP_DIR, None
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)


def sweep_stale_temp_files(max_age_seconds: float = 3600.0) -> int:
    """Delete untracked temp files older than ``max_age_seconds``; returns count."""
    directory = _TEMP_DIR
    if directory is None or not directory.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age_seconds
    with _TEMP_LOCK:
        active = set(_ACTIVE_TEMP_FILES)
    for entry in directory.iterdir():
        try:
            if str(entry) not in active and entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


atexit.register(cleanup_temp_files)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def _sanitize_page_text(raw: str) -> str:
    """Normalise line endings and drop control characters (offsets computed afterwards)."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS.sub("", text)


def _has_alnum(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


class ResumeParser:
    """Validate a PDF and extract page-level text.

    The parser is stateless and thread-safe. ``parse`` is synchronous and
    CPU-bound; call it from the worker (or ``asyncio.to_thread``), never inline
    in an upload handler for large files.
    """

    def __init__(self, limits: ParserLimits | None = None, *, use_temp_file: bool = True) -> None:
        self.limits = limits or ParserLimits.from_env()
        self._use_temp_file = use_temp_file

    # -- public API ---------------------------------------------------------- #
    def check_bytes(self, data: bytes) -> None:
        """Cheap checks (no PDF parsing): emptiness, size, signature."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("PDF data must be bytes")
        size = len(data)
        if size == 0:
            raise EmptyPDFError()
        if size > self.limits.max_bytes:
            raise PDFTooLargeError(details={"max_bytes": self.limits.max_bytes})
        if bytes(data[:_SIGNATURE_WINDOW]).find(_SIGNATURE) == -1:
            raise InvalidPDFSignatureError()

    def parse(self, data: bytes) -> ParsedDocument:
        """Validate and extract text. Raises ``PDFValidationError`` subclasses."""
        data = bytes(data) if not isinstance(data, bytes) else data
        deadline = time.monotonic() + self.limits.timeout_seconds
        self.check_bytes(data)
        digest = hashlib.sha256(data).hexdigest()

        if self._use_temp_file:
            with secure_temp_pdf(data) as path:
                document = self._open(path=path)
                try:
                    result = self._build(document, digest, len(data), deadline)
                finally:
                    _close_quietly(document)
        else:
            document = self._open(data=data)
            try:
                result = self._build(document, digest, len(data), deadline)
            finally:
                _close_quietly(document)

        logger.info(
            "pdf_parsed",
            extra={
                "sha256_prefix": digest[:12],
                "pages": result.page_count,
                "chars": result.char_count,
                "image_only_pages": len(result.image_only_pages),
            },
        )
        return result

    # -- internals ----------------------------------------------------------- #
    @staticmethod
    def _open(*, path: str | None = None, data: bytes | None = None):
        try:
            if path is not None:
                return pymupdf.open(path, filetype="pdf")
            return pymupdf.open(stream=data, filetype="pdf")
        except Exception:  # PyMuPDF raises FileDataError/RuntimeError/ValueError
            raise MalformedPDFError() from None

    def _build(self, doc, digest: str, size: int, deadline: float) -> ParsedDocument:
        limits = self.limits
        try:
            needs_pass = bool(doc.needs_pass)
        except Exception:
            raise MalformedPDFError() from None
        if needs_pass:
            raise EncryptedPDFError()

        try:
            page_count = int(doc.page_count)
            is_encrypted = bool(doc.is_encrypted)
            was_repaired = bool(getattr(doc, "is_repaired", False))
        except Exception:
            raise MalformedPDFError() from None
        if page_count <= 0:
            raise MalformedPDFError("The PDF contains no readable pages.")
        if page_count > limits.max_pages:
            raise PDFPageLimitError(
                details={"max_pages": limits.max_pages, "page_count": page_count}
            )

        warnings: list[str] = []
        if is_encrypted:
            # Opened without a password (owner-password/permissions only).
            warnings.append("PDF_ENCRYPTED_NO_USER_PASSWORD")
        if was_repaired:
            warnings.append("PDF_REPAIRED")

        texts: list[str] = []
        image_only: list[int] = []
        total_chars = 0
        for index in range(page_count):
            if time.monotonic() > deadline:
                raise PDFProcessingLimitError(details={"timeout_seconds": limits.timeout_seconds})
            try:
                page = doc.load_page(index)
                text = _sanitize_page_text(page.get_text("text"))
                has_images = False
                if not _has_alnum(text):
                    has_images = len(page.get_images(full=False)) > 0
            except Exception:
                raise MalformedPDFError() from None
            if PAGE_SEPARATOR in text:  # defensive; sanitiser already removed \f
                text = text.replace(PAGE_SEPARATOR, "")
            total_chars += len(text)
            if total_chars > limits.max_text_chars:
                raise PDFTextTooLargeError(details={"max_text_chars": limits.max_text_chars})
            if has_images:
                image_only.append(index + 1)
            texts.append(text)

        if time.monotonic() > deadline:
            raise PDFProcessingLimitError(details={"timeout_seconds": limits.timeout_seconds})

        meaningful = sum(1 for text in texts for ch in text if ch.isalnum())
        if meaningful == 0 or meaningful < limits.min_extracted_chars:
            if image_only:
                raise OCRRequiredError(details={"image_only_pages": image_only})
            raise NoExtractableTextError()

        if image_only:
            warnings.append("PARTIAL_IMAGE_ONLY_PAGES")

        return ParsedDocument.from_page_texts(
            texts,
            sha256=digest,
            byte_size=size,
            image_only_pages=image_only,
            warnings=warnings,
            is_encrypted=is_encrypted,
            was_repaired=was_repaired,
        )


def _close_quietly(document) -> None:
    try:
        document.close()
    except Exception:  # pragma: no cover - defensive
        pass


def parse_pdf_bytes(data: bytes, limits: ParserLimits | None = None) -> ParsedDocument:
    """Convenience wrapper: ``ResumeParser(limits).parse(data)``."""
    return ResumeParser(limits).parse(data)