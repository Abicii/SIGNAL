import asyncio
import json

import httpx
import pytest

from service.gemini import (
    GEMINI_ENDPOINT,
    GEMINI_MODEL,
    MAX_CONTEXT_BYTES,
    GeminiLLMProvider,
    GeminiMalformedOutputError,
    GeminiProviderError,
)
from service.llm import LLMDecision
from service.replan import ReplanService
from tests.test_decision_engine import canonical_state
from tests.test_llm_adjudication import ambiguous_state
from tests.test_replan_service import request_from_state
from signal_engine.engine import run_planner


def _context(state=None):
    state = state or ambiguous_state()
    pipeline = run_planner(state)
    from service.llm import build_ai_context

    return state, pipeline, build_ai_context(
        state, pipeline.normalized_state, pipeline.constraints, pipeline.conflicts,
        pipeline.candidates, pipeline.scores,
    )


def _response(decision: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(decision)}]}}]}


def test_gemini_request_uses_allowlisted_context_schema_and_api_key_header():
    state = canonical_state(life_events=(canonical_state().life_events[0].model_copy(update={
        "kind": "Alex Smith alex@example.com 555-0100 1 Example Road account-raw-session-id",
    }),))
    state, pipeline, context = _context(state)
    captured = {}
    selected_id = context.valid_candidates[0].candidate_id

    async def handler(request):
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response({"selected_candidate_id": selected_id, "reason_codes": []}))

    transport = httpx.MockTransport(handler)
    provider = GeminiLLMProvider("unit-test-secret", transport=transport)
    result = asyncio.run(provider.generate_structured(context))

    request = captured["request"]
    body = captured["body"]
    prompt = body["contents"][0]["parts"][0]["text"]
    assert str(request.url) == GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
    assert request.headers["x-goog-api-key"] == "unit-test-secret"
    assert "unit-test-secret" not in request.url.query.decode()
    assert body["generationConfig"]["temperature"] == 0
    response_format = body["generationConfig"]["responseFormat"]["text"]
    assert response_format["mimeType"] == "APPLICATION_JSON"
    schema = response_format["schema"]
    assert schema["required"] == ["selected_candidate_id", "reason_codes"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"selected_candidate_id", "reason_codes"}
    for instruction in ("already generated and validated", "only return reason_codes", "Do not create", "required JSON schema"):
        assert instruction in prompt
    for private in ("Alex Smith", "alex@example.com", "555-0100", "1 Example Road", "account-raw-session-id"):
        assert private not in prompt
    assert "logical_session_id" not in prompt
    assert "attributes" not in prompt
    assert result == LLMDecision(selected_candidate_id=selected_id, reason_codes=())
    assert len(context.valid_candidates) == len(pipeline.scores)


def test_gemini_invalid_candidate_is_rejected_by_application_membership_check():
    state, pipeline, context = _context()

    async def handler(_request):
        return httpx.Response(200, json=_response({"selected_candidate_id": "cand-from-another-run", "reason_codes": []}))

    result = ReplanService(provider=GeminiLLMProvider(
        "unit-test-secret", transport=httpx.MockTransport(handler),
    )).replan(request_from_state(state))
    assert result.validation.valid
    assert result.selected_candidate.candidate_id == pipeline.decision.selected_candidate_id
    assert result.adjudication.fallback_code == "UNKNOWN_OR_INVALID_CANDIDATE"
    assert result.adjudication.provider == "gemini"
    assert result.adjudication.model_name == GEMINI_MODEL


def test_gemini_reason_code_is_checked_against_selection_trace():
    state, pipeline, context = _context()
    selected_id = pipeline.decision.selected_candidate_id

    async def handler(_request):
        return httpx.Response(200, json=_response({
            "selected_candidate_id": selected_id,
            "reason_codes": ["REDUCE_OVERLOAD"],
        }))

    result = ReplanService(provider=GeminiLLMProvider(
        "unit-test-secret", transport=httpx.MockTransport(handler),
    )).replan(request_from_state(state))
    assert result.validation.valid
    assert result.selected_candidate.candidate_id == selected_id
    assert result.adjudication.fallback_code == "UNSUPPORTED_REASON_CODE"


def test_gemini_malformed_structured_output_is_rejected():
    _, _, context = _context()

    async def handler(_request):
        return httpx.Response(200, json=_response({"selected_candidate_id": "cand-x", "reason_codes": ["NOT_REGISTERED"]}))

    with pytest.raises(GeminiMalformedOutputError, match="malformed structured output"):
        asyncio.run(GeminiLLMProvider(
            "unit-test-secret", transport=httpx.MockTransport(handler),
        ).generate_structured(context))


def test_gemini_http_error_and_timeout_are_sanitized():
    _, _, context = _context()

    async def http_error(_request):
        return httpx.Response(429, json={"error": {
            "status": "RESOURCE_EXHAUSTED",
            "message": "unit-test-secret was rejected; private provider details",
        }})

    with pytest.raises(GeminiProviderError, match="Gemini request failed") as error:
        asyncio.run(GeminiLLMProvider(
            "unit-test-secret", transport=httpx.MockTransport(http_error),
        ).generate_structured(context))
    assert "secret body" not in str(error.value)
    assert "unit-test-secret" not in str(error.value)
    diagnostic = error.value.diagnostics.as_dict()
    assert diagnostic["http_status_code"] == 429
    assert diagnostic["api_error_category"] == "RESOURCE_EXHAUSTED"
    assert "unit-test-secret" not in diagnostic["sanitized_error_message"]
    assert diagnostic["api_key_present"] is True
    assert diagnostic["endpoint"] == "generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    assert "secret body" not in str(diagnostic)

    async def timeout(_request):
        raise httpx.ReadTimeout("private transport detail")

    with pytest.raises(TimeoutError, match="Gemini request timed out") as error:
        asyncio.run(GeminiLLMProvider(
            "unit-test-secret", transport=httpx.MockTransport(timeout),
        ).generate_structured(context))
    assert "private transport detail" not in str(error.value)
    assert error.value.diagnostics.exception_type == "ReadTimeout"


def test_gemini_missing_key_and_context_limit_fail_safely():
    _, _, context = _context()
    with pytest.raises(GeminiProviderError, match="API key is not configured"):
        asyncio.run(GeminiLLMProvider(None).generate_structured(context))
    oversized = context.model_copy(update={
        "valid_candidates": context.valid_candidates * (MAX_CONTEXT_BYTES // 100 + 1),
    })
    with pytest.raises(GeminiProviderError, match="size limit"):
        asyncio.run(GeminiLLMProvider("unit-test-secret").generate_structured(oversized))


def test_gemini_provider_failure_uses_existing_deterministic_fallback():
    state, pipeline, _ = _context()

    async def unavailable(_request):
        raise httpx.ConnectError("private network details")

    result = ReplanService(provider=GeminiLLMProvider(
        "unit-test-secret", transport=httpx.MockTransport(unavailable),
    )).replan(request_from_state(state))
    assert result.validation.valid
    assert result.selected_candidate.candidate_id == pipeline.decision.selected_candidate_id
    assert result.adjudication.adjudication_attempted
    assert result.adjudication.fallback_used
    assert result.adjudication.fallback_code == "PROVIDER_FAILURE"

    async def timed_out(_request):
        raise httpx.ReadTimeout("private timeout detail")

    timeout_result = ReplanService(provider=GeminiLLMProvider(
        "unit-test-secret", transport=httpx.MockTransport(timed_out),
    )).replan(request_from_state(state))
    assert timeout_result.validation.valid
    assert timeout_result.selected_candidate.candidate_id == pipeline.decision.selected_candidate_id
    assert timeout_result.adjudication.fallback_used
    assert timeout_result.adjudication.fallback_code == "PROVIDER_TIMEOUT"
