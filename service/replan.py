from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

from pydantic import ValidationError

from signal_engine.engine import run_planner
from signal_engine.models import CurrentState, PipelineResult, ValidationResult

from .models import (
    AdjudicationTrace,
    DecisionExplanation,
    DecisionInputSnapshot,
    ReplanRequest,
    ReplanResult,
    ReplanStatus,
    build_changes,
    empty_result,
    error_result,
)
from .llm import LLMProvider, adjudicate


Planner = Callable[[CurrentState], PipelineResult]


def _validation_messages(error: ValidationError) -> tuple[str, ...]:
    return tuple(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in error.errors(include_url=False)
    )


def _input_error_status(error: ValidationError) -> ReplanStatus:
    locations = {str(part) for item in error.errors(include_url=False) for part in item["loc"]}
    if locations.intersection({"schedule_window", "starts_at", "ends_at", "start_at", "end_at"}):
        return ReplanStatus.INVALID_SCHEDULE_WINDOW
    return ReplanStatus.INVALID_INPUT


class ReplanService:
    """Validate application input, call the pure engine, and assemble a result."""

    def __init__(self, planner: Planner = run_planner, *, provider: LLMProvider | None = None, provider_timeout: float = 1.8) -> None:
        self._planner = planner
        self._provider = provider
        self._provider_timeout = provider_timeout

    def replan(self, application_input: ReplanRequest | Mapping[str, object]) -> ReplanResult:
        try:
            request = application_input if isinstance(application_input, ReplanRequest) else ReplanRequest.model_validate(application_input)
        except ValidationError as exc:
            return error_result(_input_error_status(exc), _validation_messages(exc))

        if request.current_plan is None:
            return error_result(ReplanStatus.NO_CURRENT_PLAN, ("current_plan is required",))

        try:
            domain_state = request.to_domain()
        except ValidationError as exc:
            return error_result(_input_error_status(exc), _validation_messages(exc))

        session_ids = {session.logical_session_id for session in domain_state.current_plan}
        if any(not domain_state.horizon_start <= session.local_date <= domain_state.horizon_end for session in domain_state.current_plan):
            return error_result(
                ReplanStatus.INCONSISTENT_STATE,
                ("current plan contains a session outside the requested horizon",),
            )
        unknown_preserved = set(domain_state.must_preserve_session_ids) - session_ids
        if unknown_preserved:
            return error_result(
                ReplanStatus.INCONSISTENT_STATE,
                ("must_preserve_session_ids references a session absent from current_plan",),
            )

        pipeline = self._planner(domain_state)
        if not pipeline.validation.valid:
            status = (
                ReplanStatus.NO_VALID_CANDIDATE
                if "NO_VALID_CANDIDATE" in pipeline.validation.violations
                else ReplanStatus.ENGINE_VALIDATION_FAILED
            )
            return ReplanResult(
                status=status,
                original_plan=domain_state.current_plan,
                candidate_plans=pipeline.candidates,
                candidate_scores=pipeline.scores,
                conflicts=pipeline.conflicts,
                validation=pipeline.validation,
                explanation=DecisionExplanation(conflicts=pipeline.conflicts),
                input_snapshot=_snapshot(request),
            )

        if pipeline.decision is None:
            failed_validation = ValidationResult(
                valid=False,
                violations=("MISSING_PLANNER_DECISION",),
                validator_version=pipeline.validation.validator_version,
                candidate_content_hash=pipeline.validation.candidate_content_hash,
            )
            return ReplanResult(
                status=ReplanStatus.ENGINE_VALIDATION_FAILED,
                original_plan=domain_state.current_plan,
                candidate_plans=pipeline.candidates,
                candidate_scores=pipeline.scores,
                conflicts=pipeline.conflicts,
                validation=failed_validation,
                explanation=DecisionExplanation(conflicts=pipeline.conflicts),
                input_snapshot=_snapshot(request),
            )

        decision = pipeline.decision
        validation = pipeline.validation
        adjudication_trace = AdjudicationTrace()
        if self._provider is not None:
            ai_decision, ai_validation, adjudication_trace = adjudicate(
                self._provider,
                timeout=self._provider_timeout,
                state=domain_state,
                normalized=pipeline.normalized_state,
                constraints=pipeline.constraints,
                conflicts=pipeline.conflicts,
                candidates=pipeline.candidates,
                scores=pipeline.scores,
            )
            if ai_decision is not None and ai_validation is not None:
                decision = ai_decision
                validation = ai_validation

        candidate = next(
            (candidate for candidate in pipeline.candidates if candidate.candidate_id == decision.selected_candidate_id),
            None,
        )
        if candidate is None or not candidate.valid:
            failed_validation = ValidationResult(
                valid=False,
                violations=("SELECTED_CANDIDATE_NOT_IN_VALID_GENERATED_SET",),
                validator_version=pipeline.validation.validator_version,
                candidate_content_hash=pipeline.validation.candidate_content_hash,
            )
            return ReplanResult(
                status=ReplanStatus.ENGINE_VALIDATION_FAILED,
                original_plan=domain_state.current_plan,
                candidate_plans=pipeline.candidates,
                candidate_scores=pipeline.scores,
                conflicts=pipeline.conflicts,
                validation=failed_validation,
                explanation=DecisionExplanation(conflicts=pipeline.conflicts),
                input_snapshot=_snapshot(request),
            )

        selected_score = next((score for score in pipeline.scores if score.candidate_id == candidate.candidate_id), None)
        changes = build_changes(domain_state.current_plan, candidate.sessions)
        reason_codes = tuple(dict.fromkeys((*decision.rationale_codes, *candidate.reason_codes)))
        return ReplanResult(
            status=ReplanStatus.PROPOSED,
            original_plan=domain_state.current_plan,
            proposed_plan=candidate.sessions,
            selected_candidate=candidate,
            candidate_plans=pipeline.candidates,
            candidate_scores=pipeline.scores,
            conflicts=pipeline.conflicts,
            changes=changes,
            reason_codes=reason_codes,
            validation=validation,
            explanation=DecisionExplanation(
                reason_codes=reason_codes,
                changes=changes,
                conflicts=pipeline.conflicts,
                selected_score=selected_score,
            ),
            adjudication=adjudication_trace,
            input_snapshot=_snapshot(request),
        )


def _snapshot(request: ReplanRequest) -> DecisionInputSnapshot:
    state = request.current_state
    return DecisionInputSnapshot(
        as_of=state.as_of,
        timezone=state.timezone,
        horizon_start=state.horizon_start,
        horizon_end=state.horizon_end,
        original_plan=request.current_plan or (),
        goals=request.goals,
        preferences=request.preferences,
        life_event=request.life_event,
        recent_history=state.recent_history,
        available_windows=state.available_windows,
        must_preserve_session_ids=state.must_preserve_session_ids,
    )


def replan(application_input: ReplanRequest | Mapping[str, object]) -> ReplanResult:
    """Convenience entry point for application code."""
    return ReplanService().replan(application_input)
