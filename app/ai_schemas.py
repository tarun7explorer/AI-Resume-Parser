"""Strict structured-response models shared by inference and the pipeline."""
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.resume_parser import PageText
from app.scoring import CategoryKey

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class AICategory(StrictModel):
    applicable: bool = True
    score: float | None = Field(default=None, ge=0, le=10, allow_inf_nan=False)
    confidence: str = "low"
    rationale: str = ""

class ProposedClaim(StrictModel):
    category: str = Field(min_length=1, max_length=64)
    claim: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=2000)
    page_hint: int | None = Field(default=None, ge=1)
    is_strength: bool = False

class AIAssessment(StrictModel):
    categories: dict[str, AICategory]
    claims: list[ProposedClaim] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_response(cls, value: Any) -> Any:
        if not isinstance(value, dict): return value
        cats = value.get("categories")
        claims = list(value.get("claims", [])) if isinstance(value.get("claims", []), list) else []
        if isinstance(cats, list):
            mapped = {}
            for entry in cats:
                if not isinstance(entry, dict): continue
                key = entry.get("category")
                if key in {x.value for x in CategoryKey}:
                    if key in mapped: raise ValueError("duplicate category")
                    mapped[key] = {k:v for k,v in entry.items() if k not in ("category", "evidence", "missing_or_uncertain")}
                    for ev in entry.get("evidence", []) if isinstance(entry.get("evidence", []), list) else []:
                        if isinstance(ev, dict): claims.append({"category":key,"claim":ev.get("claim",""),"quote":ev.get("quote",""),"page_hint":ev.get("page"),"is_strength":False})
            value = dict(value); value["categories"] = mapped; value["claims"] = claims
        if "strengths" in value:
            for ev in value.get("strengths", []) if isinstance(value.get("strengths"), list) else []:
                if isinstance(ev, dict): claims.append({"category":"experience_projects","claim":ev.get("claim",""),"quote":ev.get("quote",""),"page_hint":ev.get("page"),"is_strength":True})
            value = dict(value); value["claims"] = claims
        value = dict(value)
        value["missing_requirements"] = value.get("missing_requirements", value.get("missing_or_uncertain", []))
        return value

    @model_validator(mode="after")
    def validate_categories(self):
        expected = {x.value for x in CategoryKey}
        if set(self.categories) != expected: raise ValueError("exactly five scoring categories are required")
        for cat in self.categories.values():
            if cat.applicable and cat.score is None: raise ValueError("applicable categories require a score")
            if not cat.applicable and cat.score is not None: raise ValueError("inapplicable categories require null score")
        return self

class AIAssessmentResult(StrictModel):
    assessment: AIAssessment
    provider: str
    model: str
    prompt_version: str
    attempts: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

