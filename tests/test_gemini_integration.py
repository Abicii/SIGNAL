import os
import json

import pytest
from dotenv import load_dotenv

from service.gemini import GeminiLLMProvider
from service.llm import LLMDecision
from service.replan import ReplanService
from signal_engine.engine import run_planner
from tests.test_llm_adjudication import ambiguous_state
from tests.test_replan_service import request_from_state

load_dotenv()


class _CaptureStructuredResponse:
    """Capture only the parsed contract object; never log prompt or response text."""

    provider_name = "gemini"

    def __init__(self, delegate: GeminiLLMProvider):
        self.delegate = delegate
        self.model_name = delegate.model_name
        self.structured_response: LLMDecision | None = None

    @property
    def safe_diagnostics(self):
        return self.delegate.last_diagnostic.as_dict()

    async def generate_structured(self, context):
        self.structured_response = await self.delegate.generate_structured(context)
        return self.structured_response


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY", "").strip(), reason="GEMINI_API_KEY is not configured")
def test_live_gemini_adjudicates_ambiguous_candidates():
    assert os.environ.get("SIGNAL_LLM_PROVIDER", "").lower() == "gemini"
    state = ambiguous_state()
    pipeline = run_planner(state)
    valid_candidate_ids = {candidate.candidate_id for candidate in pipeline.candidates if candidate.valid}
    assert len(valid_candidate_ids) >= 2

    provider = _CaptureStructuredResponse(GeminiLLMProvider(
        os.environ["GEMINI_API_KEY"], timeout_seconds=20.0,
    ))
    result = ReplanService(provider=provider, provider_timeout=21.0).replan(request_from_state(state))

    assert result.adjudication.adjudication_attempted
    if not result.adjudication.adjudication_used:
        pytest.fail(
            "Gemini did not return an accepted structured decision. "
            f"fallback_code={result.adjudication.fallback_code}; "
            f"safe_diagnostics={json.dumps(provider.safe_diagnostics, sort_keys=True)}"
        )
    assert not result.adjudication.fallback_used
    assert isinstance(provider.structured_response, LLMDecision)
    assert provider.structured_response.selected_candidate_id in valid_candidate_ids
    assert result.selected_candidate is not None
    assert result.selected_candidate.candidate_id == provider.structured_response.selected_candidate_id
    # ReplanService's adjudication path reruns deterministic hard-constraint and
    # candidate-content validation after parsing Gemini's structured response.
    assert result.validation.valid
    assert result.validation.candidate_content_hash == result.selected_candidate.content_hash
