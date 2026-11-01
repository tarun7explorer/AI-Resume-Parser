import json

import httpx
import pytest

from app.ai_provider import AIAuthenticationError, AIProvider, AISettings, AIMalformedJSONError
from app.resume_parser import PageText


def settings(**extra):
    env = {
        "AI_PROVIDER": "groq",
        "AI_MODEL": "unit-test-model",
        "GROQ_API_KEY": "unit-test-secret",
        "AI_MAX_ATTEMPTS": "1",
    }
    env.update(extra)
    return AISettings.from_env(env)


def response_payload(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}


def assessment_json():
    categories = []
    for key in ("required_skills", "experience_projects", "responsibilities_capabilities", "qualifications", "evidence_strength"):
        categories.append({
            "category": key, "applicable": True, "score": 5,
            "confidence": "medium", "rationale": "Supported by the quoted source text.",
            "evidence": [{"claim": "Relevant experience", "quote": "Built reporting pipelines with Python", "page": 1}],
            "missing_or_uncertain": [],
        })
    return json.dumps({"categories": categories, "strengths": [], "missing_or_uncertain": []})


def test_provider_validates_structured_output_without_total_score():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_payload(assessment_json())))
    with httpx.Client(transport=transport) as client:
        provider = AIProvider(settings(), client=client)
        result = provider.assess_resume("A sufficiently detailed job description", [PageText(1, "Built reporting pipelines with Python", 0, 38)])
    assert result.assessment.categories["required_skills"].score == 5
    assert len(result.assessment.claims) == 5
    assert not hasattr(result.assessment, "score_out_of_10")


def test_provider_auth_error_does_not_echo_response_or_secret():
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "unit-test-secret"}))
    with httpx.Client(transport=transport) as client:
        provider = AIProvider(settings(), client=client)
        with pytest.raises(AIAuthenticationError) as exc:
            provider.assess_resume("A sufficiently detailed job description", [PageText(1, "Some resume text", 0, 16)])
    assert str(exc.value) == "ai_auth_failed"
    assert "unit-test-secret" not in str(exc.value)


def test_provider_rejects_malformed_structured_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_payload("not-json")))
    with httpx.Client(transport=transport) as client:
        provider = AIProvider(settings(), client=client)
        with pytest.raises(AIMalformedJSONError):
            provider.assess_resume("A sufficiently detailed job description", [PageText(1, "Some resume text", 0, 16)])
