"""Conservative, observable PDF integrity signals for human review."""

from __future__ import annotations

import re

from app.report_models import IntegrityFinding
from app.resume_parser import ParsedDocument

INSTRUCTION_PATTERNS = (
    re.compile(r"\bignore (?:all )?(?:previous|prior) instructions\b", re.I),
    re.compile(r"\b(?:system prompt|developer message)\b", re.I),
    re.compile(r"\b(?:assign|give) (?:me|the candidate) (?:a )?(?:perfect|maximum|highest) score\b", re.I),
    re.compile(r"\bdo not mention (?:this|these) instructions\b", re.I),
)


def analyze_integrity(document: ParsedDocument) -> list[IntegrityFinding]:
    findings: list[IntegrityFinding] = []
    seen: set[tuple[str, int, str]] = set()

    def add(signal: str, page: int, excerpt: str, explanation: str, confidence: str) -> None:
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:300]
        key = (signal, page, excerpt)
        if excerpt and key not in seen and len(findings) < 50:
            seen.add(key)
            findings.append(IntegrityFinding(
                signal=signal, page=page, excerpt=excerpt,
                explanation=explanation, confidence=confidence,
            ))

    for span in document.spans:
        if 0 < span.font_size < 3.5:
            add(
                "very_small_text", span.page, span.text,
                "Text was extracted with a font size below 3.5 points. Small text can be intentional or an accessibility artifact; review the rendered page.",
                "moderate" if span.font_size < 2 else "low",
            )
        x0, y0, x1, y1 = span.bbox
        if x1 < -2 or y1 < -2 or x0 > span.page_width + 2 or y0 > span.page_height + 2:
            add(
                "off_page_text", span.page, span.text,
                "Extracted text coordinates extend beyond the page bounds. Rendering or document conversion can also cause this signal.",
                "low",
            )
        for pattern in INSTRUCTION_PATTERNS:
            if pattern.search(span.text):
                add(
                    "instruction_like_text", span.page, span.text,
                    "Text resembles an instruction to an automated system. It is treated only as document content and is not evidence of misconduct.",
                    "low",
                )
                break

    # OCR pages have no selectable-text layout spans; inspect their extracted
    # text for instruction-like content while retaining the same cautious label.
    for page in document.pages:
        for pattern in INSTRUCTION_PATTERNS:
            match = pattern.search(page.text)
            if match:
                start = max(0, match.start() - 100)
                end = min(len(page.text), match.end() + 100)
                add(
                    "instruction_like_text", page.page, page.text[start:end],
                    "Text resembles an instruction to an automated system. It is treated only as document content and is not evidence of misconduct.",
                    "low",
                )
                break

    return findings
