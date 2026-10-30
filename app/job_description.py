"""Bounded extraction for recruiter-provided job description PDFs."""

from __future__ import annotations

from app.exceptions import ApiError


def extract_job_description_pdf(content: bytes, *, max_pages: int = 100) -> str:
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ApiError(400, "invalid_job_description_pdf", "The uploaded file is not a complete PDF")
    try:
        import fitz
    except ImportError:
        raise ApiError(503, "pdf_extraction_unavailable", "PDF extraction is not available on this server") from None

    try:
        document = fitz.open(stream=content, filetype="pdf")
        if document.is_encrypted:
            raise ApiError(422, "encrypted_pdf", "Password-protected PDFs are not supported")
        if len(document) == 0 or len(document) > max_pages:
            raise ApiError(422, "invalid_pdf_page_count", f"PDFs must contain 1 to {max_pages} pages")
        extracted = "\n\n".join(page.get_text("text") for page in document).strip()
        document.close()
    except ApiError:
        raise
    except Exception:
        raise ApiError(422, "pdf_extraction_failed", "The job description PDF could not be read") from None

    if not extracted:
        raise ApiError(422, "pdf_has_no_text", "No selectable text was found; paste the job description instead")
    if len(extracted) > 100_000:
        raise ApiError(413, "job_description_too_long", "The extracted job description exceeds 100,000 characters")
    return extracted
