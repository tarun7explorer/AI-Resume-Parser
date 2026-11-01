"""Pipeline protocol and data types; keep service boundaries explicit."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Sequence, Any
from app.ai_schemas import AIAssessment, ProposedClaim
from app.resume_parser import PageText, ParsedDocument

class ScreeningError(Exception):
    def __init__(self, code="screening_failed", safe_message="Screening failed.", *, retryable=False):
        super().__init__(safe_message); self.code=code; self.safe_message=safe_message; self.retryable=retryable
class JobInputError(ScreeningError): pass
class ResumeRejectedError(ScreeningError): pass
class AIOutputInvalidError(ScreeningError): pass
class PersistenceError(ScreeningError): pass
class LeaseLostError(ScreeningError): pass
class ScreeningInterrupted(ScreeningError): pass

@dataclass(frozen=True)
class ResumeInput:
    data: bytes
    filename: str = "resume.pdf"
    label: str = ""

@dataclass(frozen=True)
class JobInputs:
    job_description_text: str | None
    job_description_pdf: bytes | None
    resumes: tuple[ResumeInput, ...]

class ResumeParserPort(Protocol):
    def parse_pdf(self, data: bytes, *, filename: str, kind: str) -> ParsedDocument: ...
class AIAssessorPort(Protocol):
    def assess(self, *, job_description: str, resume_pages: Sequence[PageText], request_id: str) -> AIAssessment: ...
class EvidenceVerifierPort(Protocol):
    def verify_quote(self, quote: str, pages: Sequence[PageText], *, page_hint: int | None = None): ...
class ScreeningRepositoryPort(Protocol):
    def load_inputs(self, job_id: str) -> JobInputs: ...
    def save_result(self, job_id: str, result: dict[str, Any], *, worker_id: str) -> bool: ...
