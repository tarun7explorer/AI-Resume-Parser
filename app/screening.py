"""Screening pipeline orchestration: Parser -> AI -> Evidence -> Scoring -> Persistence.

Independent of HTTP. The worker calls `run_screening_job(...)`.

Rules enforced here
-------------------
* Every resume is processed independently against the same job description.
* Displayed quotes are the verifier's SOURCE text, never the model's wording; a quote
  must also be re-found (whitespace-collapsed) in the extracted pages, otherwise it is dropped.
  Quotes spanning pages are not accepted (conservative).
* Pages are shown only when the verifier marks them reliable AND the quote is on that page.
* A category scored by the AI but without any verified supporting quote becomes
  MISSING_EVIDENCE (weight kept, contributes 0, reported as "not found in extracted text").
  Evidence strength needs at least one verified quote overall. Tunable policy.
* "Not applicable" is accepted only for categories scoring.NOT_APPLICABLE_ELIGIBLE;
  otherwise the AI output is rejected. Applicability is asserted by the model (documented limitation).
* No keyword fallback and no invented scores: failures are explicit.
* Error handling: retryable errors (provider outage, persistence, interruption) abort the
  job so the worker can retry; per-resume permanent problems yield a "failed" entry for
  that resume only; if no resume could be scored the job fails.
* Evidence coverage = verified distinct claims / proposed distinct claims (scoring.compute_evidence_coverage).
* No hiring decision is produced.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from app.pipeline_types import (
    AIAssessment, AIAssessorPort, AIOutputInvalidError, EvidenceVerifierPort, JobInputError,
    JobInputs, LeaseLostError, PageText, ParsedDocument, PersistenceError, ProposedClaim,
    ResumeInput, ResumeParserPort, ResumeRejectedError, ScreeningError, ScreeningInterrupted,
    ScreeningRepositoryPort,
)
from app.scoring import (
    CATEGORY_LABELS, SCORING_VERSION, CategoryInput, CategoryKey, CategoryStatus, ScoringError,
    compute_evidence_coverage, compute_score, validate_score,
)

logger = logging.getLogger("resume_parser.screening")

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_VALID_CATEGORIES = {k.value for k in CategoryKey}

DEFAULT_LIMITATIONS = (
    "Decision support only; this is not a hiring probability or a hiring decision.",
    "Human review is required. Absence of evidence in extracted text does not prove a candidate lacks a skill.",
)


@dataclass(frozen=True)
class PipelineConfig:
    min_extracted_chars: int = 200      # below this a PDF is treated as scanned/empty
    low_text_chars: int = 800           # below this, add an extraction-quality caveat
    max_jd_chars: int = 20_000
    max_claims_per_resume: int = 40
    max_quote_chars: int = 600
    max_text_item_chars: int = 400
    max_missing_items: int = 15


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _clean(value: Any, limit: int) -> str:
    """Strip control characters, collapse whitespace, truncate. Frontend must still use textContent."""
    if not isinstance(value, str):
        return ""
    return _collapse(_CTRL.sub(" ", value))[:limit]


def _guard(fn: Callable[[], Any], code: str, message: str, *, retryable: bool = False) -> Any:
    """Run a port call; known ScreeningErrors propagate, anything else becomes a sanitized error."""
    try:
        return fn()
    except ScreeningError:
        raise
    except Exception as exc:
        from app.ai_provider import AIProviderError
        if isinstance(exc, AIProviderError):
            raise ScreeningError(exc.code, exc.public_message, retryable=exc.retryable) from None
        logger.error("unexpected_error code=%s type=%s", code, type(exc).__name__)
        raise ScreeningError(code, message, retryable=retryable) from None


class ScreeningPipeline:
    def __init__(self, *, repository: ScreeningRepositoryPort, parser: ResumeParserPort,
                 assessor: AIAssessorPort, verifier: EvidenceVerifierPort,
                 config: PipelineConfig | None = None) -> None:
        self._repo = repository
        self._parser = parser
        self._assessor = assessor
        self._verifier = verifier
        self._cfg = config or PipelineConfig()

    # ------------------------------------------------------------ job level
    def run_job(self, job_id: str, *, worker_id: str,
                should_stop: Callable[[], bool] | None = None,
                on_progress: Callable[[], None] | None = None) -> dict[str, Any]:
        """Load inputs, screen every resume, persist the result. Returns the persisted payload."""
        inputs: JobInputs = _guard(lambda: self._repo.load_inputs(job_id), "persistence_failed",
                                   "Loading job inputs failed.", retryable=True)
        jd_text = self._resolve_job_description(inputs)
        payload = self.screen_resumes(jd_text, inputs.resumes, job_id=job_id,
                                      should_stop=should_stop, on_progress=on_progress)
        saved = _guard(lambda: self._repo.save_result(job_id, payload, worker_id=worker_id),
                       "persistence_failed", "Saving the result failed.", retryable=True)
        if not saved:
            raise LeaseLostError()
        logger.info("job_completed job_id=%s scored=%s failed=%s", job_id,
                    payload["summary"]["scored"], payload["summary"]["failed"])
        return payload

    def screen_resumes(self, job_description: str, resumes: Sequence[ResumeInput], *, job_id: str,
                       should_stop: Callable[[], bool] | None = None,
                       on_progress: Callable[[], None] | None = None) -> dict[str, Any]:
        """Pure orchestration (no persistence). Each resume is independent."""
        if not resumes:
            raise JobInputError("no_resumes", "The job contains no resumes.")
        results: list[dict[str, Any]] = []
        for index, resume in enumerate(resumes, start=1):
            if should_stop and should_stop():
                raise ScreeningInterrupted()
            label = _clean(resume.label or "", 80) or f"Resume {index}"
            try:
                results.append(self._screen_one(job_description, resume, label,
                                                request_id=f"{job_id}:{index}"))
            except ResumeRejectedError as exc:  # permanent, per-resume
                logger.warning("resume_rejected job_id=%s index=%s code=%s", job_id, index, exc.code)
                results.append({"candidate_label": label, "status": "failed",
                                "error_code": exc.code, "error_message": exc.safe_message})
            if on_progress:
                on_progress()
        scored = sum(1 for r in results if r["status"] == "scored")
        if scored == 0:
            raise ScreeningError("no_resume_scored", "No resume could be scored; see per-resume errors in logs.",
                                 retryable=False)
        return {
            "results": results,
            "summary": {"total": len(results), "scored": scored, "failed": len(results) - scored},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scoring_version": SCORING_VERSION,
        }

    # ----------------------------------------------------------- job inputs
    def _resolve_job_description(self, inputs: JobInputs) -> str:
        has_text = bool(inputs.job_description_text and inputs.job_description_text.strip())
        has_pdf = bool(inputs.job_description_pdf)
        if has_text == has_pdf:
            raise JobInputError("jd_source_invalid", "Exactly one job description source is required.")
        if has_text:
            text = _collapse(_CTRL.sub(" ", inputs.job_description_text or ""))
        else:
            try:
                doc: ParsedDocument = _guard(
                    lambda: self._parser.parse_pdf(inputs.job_description_pdf or b"",
                                                   filename="job_description.pdf", kind="job_description"),
                    "jd_parse_failed", "The job description PDF could not be read.")
            except ResumeRejectedError as exc:
                raise JobInputError(exc.code, exc.safe_message) from None
            text = _collapse(_CTRL.sub(" ", "\n".join(p.text for p in doc.pages)))
        if len(text) < 20:
            raise JobInputError("jd_insufficient_text", "The job description has too little readable text.")
        if len(text) > self._cfg.max_jd_chars:
            raise JobInputError("jd_too_long", "The job description exceeds the allowed length.")
        return text

    # ------------------------------------------------------- single resume
    def _screen_one(self, jd: str, resume: ResumeInput, label: str, *, request_id: str) -> dict[str, Any]:
        cfg = self._cfg
        doc: ParsedDocument = _guard(
            lambda: self._parser.parse_pdf(resume.data, filename=resume.filename, kind="resume"),
            "resume_parse_failed", "The resume could not be read.")
        if doc.char_count < cfg.min_extracted_chars:
            raise ResumeRejectedError("insufficient_text_extracted",
                                      "Too little text could be extracted (the PDF may be scanned or image-only).")
        pages = doc.pages

        assessment = _guard(lambda: self._assessor.assess(job_description=jd, resume_pages=pages,
                                                          request_id=request_id),
                            "ai_failed", "The AI assessment failed.")
        if not isinstance(assessment, AIAssessment):
            raise AIOutputInvalidError()

        evidence, strengths, proposed, verified, supported = self._verify_claims(assessment.claims, pages)
        inputs, notes = self._category_inputs(assessment, supported, any_verified=verified > 0)
        try:
            score = compute_score(inputs)
            coverage = compute_evidence_coverage(verified, proposed)
        except ScoringError:
            raise AIOutputInvalidError() from None

        missing = [f"Not demonstrated in extracted text: {t}"
                   for t in (_clean(m, cfg.max_text_item_chars) for m in assessment.missing_requirements) if t]
        missing = missing[: cfg.max_missing_items]
        for key in score.missing_evidence:
            missing.append(f"No verifiable evidence found in extracted text for: {CATEGORY_LABELS[CategoryKey(key)]}")
        missing.extend(notes)

        limitations = list(DEFAULT_LIMITATIONS)
        if doc.char_count < cfg.low_text_chars:
            limitations.append("Little text was extracted; low scores may reflect extraction quality rather than the candidate.")
        if score.not_applicable:
            limitations.append("Some categories were marked not applicable by the model; weights were renormalised.")

        return {
            "candidate_label": label,
            "status": "scored",
            **score.to_dict(),
            "strengths": strengths,
            "missing_or_uncertain_evidence": missing,
            "evidence": evidence,
            "evidence_coverage": coverage,
            "evidence_coverage_basis": {"verified_claims": verified, "proposed_claims": proposed,
                                        "formula": "verified / proposed distinct claims"},
            "limitations": limitations,
        }

    # -------------------------------------------------------------- evidence
    def _verify_claims(self, claims: Sequence[ProposedClaim], pages: Sequence[PageText]):
        cfg = self._cfg
        page_text = {p.page_number: _collapse(p.text) for p in pages}
        evidence: list[dict[str, Any]] = []
        strengths: list[str] = []
        seen: set[tuple[str, str]] = set()
        supported: set[str] = set()
        proposed = verified = 0

        for claim in list(claims)[: cfg.max_claims_per_resume]:
            if not isinstance(claim, ProposedClaim):
                continue
            quote = _clean(claim.quote, cfg.max_quote_chars)
            statement = _clean(claim.claim, cfg.max_text_item_chars)
            result = None
            if quote and statement and claim.category in _VALID_CATEGORIES:
                result = _guard(lambda: self._verifier.verify_quote(quote, pages, page_hint=claim.page_hint),
                                "evidence_verification_failed", "Evidence verification failed.")
            source_quote = _collapse(result.source_quote) if (result and result.verified and result.source_quote) else ""
            key = (claim.category, (source_quote or quote).casefold())
            if key in seen:  # duplicate quote in same category: counted once
                continue
            seen.add(key)
            proposed += 1
            if not source_quote or len(source_quote) > cfg.max_quote_chars:
                continue
            on_page = result.page if result.page in page_text and source_quote in page_text[result.page] else None
            if on_page is None and not any(source_quote in t for t in page_text.values()):
                logger.warning("verifier_contract_violation reason=quote_not_in_source")
                continue
            verified += 1
            supported.add(claim.category)
            page = on_page if (result.page_reliable and on_page is not None) else None
            evidence.append({"claim": statement, "quote": source_quote, "page": page,
                             "category": claim.category})
            if claim.is_strength and statement not in strengths:
                strengths.append(statement)
        return evidence, strengths, proposed, verified, supported

    # -------------------------------------------------------------- scoring
    def _category_inputs(self, a: AIAssessment, supported: set[str], *, any_verified: bool):
        if not isinstance(a.categories, dict) and not hasattr(a.categories, "keys"):
            raise AIOutputInvalidError()
        if set(a.categories.keys()) != _VALID_CATEGORIES:
            raise AIOutputInvalidError("ai_invalid_categories", "The AI response did not contain exactly the required categories.")
        inputs: list[CategoryInput] = []
        notes: list[str] = []
        for key in CategoryKey:
            cat = a.categories[key.value]
            if not getattr(cat, "applicable", True):
                inputs.append(CategoryInput(key, CategoryStatus.NOT_APPLICABLE))
                continue
            try:
                value = validate_score(cat.score, field=key.value)
            except ScoringError:
                raise AIOutputInvalidError() from None
            has_support = any_verified if key is CategoryKey.EVIDENCE_STRENGTH else key.value in supported
            if has_support:
                inputs.append(CategoryInput(key, CategoryStatus.ASSESSED, float(value)))
            else:
                inputs.append(CategoryInput(key, CategoryStatus.MISSING_EVIDENCE, None))
                if value > 0:
                    notes.append(f"A model-proposed score for {CATEGORY_LABELS[key]} was discarded "
                                 "because no supporting quotation could be verified.")
        return inputs, notes


def run_screening_job(job_id: str, worker_id: str, *, repository: ScreeningRepositoryPort,
                      parser: ResumeParserPort, assessor: AIAssessorPort,
                      verifier: EvidenceVerifierPort, config: PipelineConfig | None = None,
                      should_stop: Callable[[], bool] | None = None,
                      on_progress: Callable[[], None] | None = None) -> dict[str, Any]:
    """Entry point for app/worker.py."""
    return ScreeningPipeline(repository=repository, parser=parser, assessor=assessor,
                             verifier=verifier, config=config).run_job(
        job_id, worker_id=worker_id, should_stop=should_stop, on_progress=on_progress)


def run_screening(job_id: str, inputs, correlation_id: str) -> dict[str, Any]:
    """Worker entry point: adapt durable DB inputs to the pure screening pipeline."""
    from app.ai_provider import AIProvider
    from app.evidence import EvidenceVerifier
    from app.pipeline_types import JobInputs, ResumeInput
    from app.resume_parser import ResumeParser

    jd_text = next((x.text_content for x in inputs if x.input_type == "jd_text"), None)
    jd_pdf = next((x.data for x in inputs if x.input_type == "jd_pdf"), None)
    resumes = tuple(ResumeInput(x.data or b"", x.filename or f"resume_{x.position}.pdf",
                                x.candidate_label or f"Resume {x.position}")
                    for x in inputs if x.input_type == "resume_pdf")
    class ParserAdapter:
        def parse_pdf(self, data, *, filename, kind):
            try: return parser.parse(data)
            except Exception as exc:
                from app.resume_parser import PDFValidationError
                if isinstance(exc, PDFValidationError):
                    raise ResumeRejectedError(exc.code, exc.public_message) from None
                raise
    class AssessorAdapter:
        def assess(self, *, job_description, resume_pages, request_id):
            return provider.assess_resume(job_description, resume_pages).assessment
    class EvidenceAdapter:
        def verify_quote(self, quote, pages, *, page_hint=None):
            from app.evidence import ProposedEvidence
            return EvidenceVerifier(ParsedDocument.from_page_texts([p.text for p in pages]), min_quote_chars=1).verify_one(
                ProposedEvidence("evidence", quote, page_hint))
    parser = ResumeParser()
    provider = AIProvider.from_env()
    try:
        pipeline = ScreeningPipeline(repository=None, parser=ParserAdapter(), assessor=AssessorAdapter(),
                                     verifier=EvidenceAdapter())
        jd = pipeline._resolve_job_description(JobInputs(jd_text, jd_pdf, resumes))
        return pipeline.screen_resumes(jd, resumes, job_id=correlation_id)
    finally:
        provider.close()
