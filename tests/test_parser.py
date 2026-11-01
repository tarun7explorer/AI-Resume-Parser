"""Tests for app/resume_parser.py using synthetic, generated PDFs only."""

from __future__ import annotations

import pytest

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore[no-redef]

from app.resume_parser import (
    EmptyPDFError,
    EncryptedPDFError,
    InvalidPDFSignatureError,
    MalformedPDFError,
    NoExtractableTextError,
    OCRRequiredError,
    PAGE_SEPARATOR,
    PDFPageLimitError,
    PDFProcessingLimitError,
    PDFTextTooLargeError,
    PDFTooLargeError,
    ParsedDocument,
    ParserLimits,
    ResumeParser,
    active_temp_file_count,
)

LONG_TEXT = "Experienced Python developer who built data pipelines and REST APIs for finance teams."


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 780), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def make_image_pdf(pages: int = 1, text_on_first: str = "") -> bytes:
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    pix.clear_with(200)
    for i in range(pages):
        page = doc.new_page()
        if i == 0 and text_on_first:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 400), text_on_first, fontsize=11)
            page.insert_image(pymupdf.Rect(50, 500, 150, 600), pixmap=pix)
        else:
            page.insert_image(pymupdf.Rect(50, 50, 450, 450), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(params=[True, False], ids=["temp_file", "in_memory"])
def parser(request) -> ResumeParser:
    return ResumeParser(ParserLimits(), use_temp_file=request.param)


def test_valid_multi_page_pdf_preserves_page_offsets(parser):
    data = make_pdf([LONG_TEXT + " Page one marker.", "Second page marker with more words here."])
    doc = parser.parse(data)

    assert doc.page_count == 2
    assert len(doc.sha256) == 64
    assert doc.byte_size == len(data)
    assert "Page one marker" in doc.pages[0].text
    assert "Second page marker" in doc.pages[1].text
    for page in doc.pages:
        assert doc.full_text[page.start_offset : page.end_offset] == page.text
    assert doc.full_text.count(PAGE_SEPARATOR) == 1
    assert doc.page_for_offset(doc.full_text.index("Page one marker")) == 1
    assert doc.page_for_offset(doc.full_text.index("Second page marker")) == 2
    assert doc.extraction_complete and not doc.needs_manual_review


def test_parse_is_deterministic(parser):
    data = make_pdf([LONG_TEXT])
    assert parser.parse(data).full_text == parser.parse(data).full_text


def test_empty_bytes_rejected(parser):
    with pytest.raises(EmptyPDFError):
        parser.parse(b"")


@pytest.mark.parametrize("payload", [b"PK\x03\x04 not a pdf", b"<html>%PDF-</html>" + b"x" * 2000])
def test_bad_signature_rejected(parser, payload):
    with pytest.raises(InvalidPDFSignatureError):
        parser.parse(payload)


def test_oversized_file_rejected():
    parser = ResumeParser(ParserLimits(max_bytes=500))
    with pytest.raises(PDFTooLargeError) as info:
        parser.parse(make_pdf([LONG_TEXT]))
    assert info.value.http_status == 413


def test_page_limit_rejected():
    parser = ResumeParser(ParserLimits(max_pages=2))
    with pytest.raises(PDFPageLimitError):
        parser.parse(make_pdf([LONG_TEXT] * 3))


def test_encrypted_pdf_rejected(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 400), LONG_TEXT, fontsize=11)
    target = tmp_path / "enc.pdf"
    doc.save(str(target), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.close()
    with pytest.raises(EncryptedPDFError):
        ResumeParser(ParserLimits()).parse(target.read_bytes())


def test_malformed_pdf_rejected(parser):
    with pytest.raises(MalformedPDFError):
        parser.parse(b"%PDF-1.4\n" + b"\x00\xff\x10garbage" * 200)


def test_blank_pdf_is_no_extractable_text_not_ocr(parser):
    with pytest.raises(NoExtractableTextError) as info:
        parser.parse(make_pdf(["", ""]))
    assert not isinstance(info.value, OCRRequiredError)
    assert info.value.code == "pdf_no_extractable_text"


def test_image_only_pdf_requires_ocr_and_is_not_faked(parser):
    with pytest.raises(OCRRequiredError) as info:
        parser.parse(make_image_pdf(pages=2))
    assert info.value.code == "pdf_requires_ocr"
    assert info.value.details["image_only_pages"] == [1, 2]


def test_partial_image_only_pages_are_flagged_not_hidden(parser):
    doc = parser.parse(make_image_pdf(pages=2, text_on_first=LONG_TEXT))
    assert doc.image_only_pages == (2,)
    assert doc.needs_manual_review and not doc.extraction_complete
    assert "PARTIAL_IMAGE_ONLY_PAGES" in doc.warnings
    assert doc.pages[1].text.strip() == ""


def test_text_size_limit():
    parser = ResumeParser(ParserLimits(max_text_chars=20))
    with pytest.raises(PDFTextTooLargeError):
        parser.parse(make_pdf([LONG_TEXT]))


def test_processing_time_limit():
    parser = ResumeParser(ParserLimits(timeout_seconds=1e-9))
    with pytest.raises(PDFProcessingLimitError):
        parser.parse(make_pdf([LONG_TEXT]))


def test_temp_files_removed_after_success_and_failure():
    parser = ResumeParser(ParserLimits(max_pages=1), use_temp_file=True)
    parser.parse(make_pdf([LONG_TEXT]))
    assert active_temp_file_count() == 0
    with pytest.raises(PDFPageLimitError):
        parser.parse(make_pdf([LONG_TEXT, LONG_TEXT]))
    assert active_temp_file_count() == 0
    with pytest.raises(MalformedPDFError):
        parser.parse(b"%PDF-1.4\n" + b"\x00garbage" * 100)
    assert active_temp_file_count() == 0


def test_errors_never_contain_document_content():
    parser = ResumeParser(ParserLimits(max_pages=1))
    with pytest.raises(PDFPageLimitError) as info:
        parser.parse(make_pdf(["Secret candidate text here", "Another page"]))
    assert "Secret candidate" not in str(info.value.to_dict())


def test_from_page_texts_rejects_separator_and_empty_pages_tracked():
    with pytest.raises(ValueError):
        ParsedDocument.from_page_texts(["bad\fpage"])
    doc = ParsedDocument.from_page_texts(["abc", "", "def"])
    assert doc.empty_pages == (2,)
    assert doc.pages_for_span(0, 3) == (1, 1)
    assert doc.page_for_offset(doc.full_text.index("def")) == 3


def test_limits_from_env_validation(monkeypatch):
    monkeypatch.setenv("MAX_PDF_PAGES", "abc")
    with pytest.raises(ValueError):
        ParserLimits.from_env()
    monkeypatch.setenv("MAX_PDF_PAGES", "5")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1234")
    limits = ParserLimits.from_env()
    assert (limits.max_pages, limits.max_bytes) == (5, 1234)