"""Bounded, privacy-minimized adjudication between deterministic candidates."""

from __future__ import annotations

import asyncio
import json
from time import perf_counter
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError

from signal_engine.candidates import _content_identity
from signal_engine.constraints import validate_hard_constraints
from signal_engine.explanations import derive_reason_codes
from signal_engine.models import (
    CandidatePlan,
    CandidatePlanScore,
    Conflict,
    Constraint,
    CurrentState,
    NormalizedState,
    PlannerDecision,
    ValidationResult,
)
from signal_engine.scoring import deterministic_rank_key

from .models import AdjudicationTrace, FrozenModel


class ReasonCode(StrEnum):
    PRESERVE_PRIMARY_GOAL = "PRESERVE_PRIMARY_GOAL"
    RESPECT_CURRENT_INSTRUCTION = "RESPECT_CURRENT_INSTRUCTION"
    REDUCE_RECOVERY_CONFLICT = "REDUCE_RECOVERY_CONFLICT"
    MINIMIZE_SCHEDULE_DISRUPTION = "MINIMIZE_SCHEDULE_DISRUPTION"
    RESPECT_USER_PREFERENCE = "RESPECT_USER_PREFERENCE"
    PRESERVE_TRAINING_VOLUME = "PRESERVE_TRAINING_VOLUME"
    REDUCE_OVERLOAD = "REDUCE_OVERLOAD"
    HANDLE_TIME_CONFLICT = "HANDLE_TIME_CONFLICT"


class GoalSummary(FrozenModel):
    kind: str
    priority: int


class PreferenceSummary(FrozenModel):
    days: tuple[int, ...]
    strength: str


class ContextSummary(FrozenModel):
    kind: str
    weekday: int
    duration_band: str


class RecoverySummary(FrozenModel):
    known: bool
    fit_band: str


class ConstraintSummary(FrozenModel):
    code: str
    strength: str


class ConflictSummary(FrozenModel):
    code: str
    severity: str
    affected_count: int


class SessionSummary(FrozenModel):
    activity: str
    weekday: int
    start_period: str
    duration_band: str
    intensity: str


class CandidateSummary(FrozenModel):
    candidate_id: str
    operations: tuple[str, ...]
    sessions: tuple[SessionSummary, ...]
    score: str
    goal_alignment: float
    preference_adherence: float
    schedule_fit: float
    recovery_fit: float
    training_continuity: float
    disruption_cost: float
    soft_constraint_penalty: float
    supported_reason_codes: tuple[ReasonCode, ...]


class AIContext(FrozenModel):
    """Explicit allowlist projection; intentionally has no arbitrary text/maps."""

    schema_version: str = "signal-ai-context-v1"
    horizon_days: int = Field(ge=1, le=366)
    recovery: RecoverySummary
    goal_priorities: tuple[GoalSummary, ...]
    preferences: tuple[PreferenceSummary, ...]
    event_context: tuple[ContextSummary, ...]
    constraints: tuple[ConstraintSummary, ...]
    conflicts: tuple[ConflictSummary, ...]
    valid_candidates: tuple[CandidateSummary, ...]
    allowed_rationale_codes: tuple[ReasonCode, ...]


class LLMDecision(FrozenModel):
    selected_candidate_id: str = Field(min_length=1, max_length=80)
    reason_codes: tuple[ReasonCode, ...] = Field(max_length=8)


class LLMProvider(Protocol):
    async def generate_structured(
        self,
        context: AIContext,
    ) -> LLMDecision:
        """Choose only from allowlisted candidate summaries; never edit a plan."""


class MockLLMProvider:
    """Deterministic local provider used by tests and development."""

    provider_name = "mock"
    model_name = "MockLLM"

    def __init__(self, preferred_candidate_id: str | None = None) -> None:
        self.preferred_candidate_id = preferred_candidate_id

    async def generate_structured(
        self,
        context: AIContext,
    ) -> LLMDecision:
        if not context.valid_candidates:
            raise ValueError("no valid candidates")
        selected = next(
            (item for item in context.valid_candidates if item.candidate_id == self.preferred_candidate_id),
            context.valid_candidates[0],
        )
        return LLMDecision(
            selected_candidate_id=selected.candidate_id,
            reason_codes=selected.supported_reason_codes,
        )


_SAFE_CONSTRAINTS = {
    "EVENT_UNAVAILABLE", "REDUCED_RECOVERY_WINDOW", "PRESERVE_SESSION",
    "LOWER_BODY_SPACING", "PREFERRED_TRAINING_DAYS",
}
_SAFE_CONFLICTS = {
    "RECOVERY_CONFLICT", "EVENT_UNAVAILABLE", "PREFERRED_TRAINING_DAYS",
    "LOWER_BODY_SPACING", "SCHEDULE_OVERLAP",
}
_SAFE_EVENT_KINDS = {"social", "work", "travel", "late_night", "poor_sleep", "soreness", "missed_workout"}
_SAFE_ACTIVITIES = {
    "upper": "strength", "lower": "strength", "lower_body": "strength", "strength": "strength",
    "run": "running", "running": "running", "long_run": "running", "endurance": "endurance",
    "recovery": "recovery", "rest": "rest",
}
_SAFE_OPERATIONS = {
    "MOVE_SESSION", "REMOVE_SESSION", "ADD_RECOVERY", "REDUCE_INTENSITY",
    "REDUCE_DURATION", "SWAP_SESSIONS", "PRESERVE_SESSION", "NO_CHANGE",
}


def _band(value: int | float) -> str:
    if value <= 30:
        return "short"
    if value <= 60:
        return "medium"
    return "long"


def _activity(value: str) -> str:
    return _SAFE_ACTIVITIES.get(value.lower(), "other")


def build_ai_context(
    state: CurrentState,
    normalized: NormalizedState,
    constraints: tuple[Constraint, ...],
    conflicts: tuple[Conflict, ...],
    candidates: tuple[CandidatePlan, ...],
    scores: tuple[CandidatePlanScore, ...],
) -> AIContext:
    """Project only bounded planning signals; never dump source objects."""
    candidate_by_id = {item.candidate_id: item for item in candidates if item.valid}
    score_by_id = {item.candidate_id: item for item in scores if item.valid}
    rankable = [item for key, item in score_by_id.items() if key in candidate_by_id]
    rankable.sort(key=lambda item: deterministic_rank_key(item, candidate_by_id[item.candidate_id].content_hash))
    summaries: list[CandidateSummary] = []
    for score in rankable:
        candidate = candidate_by_id[score.candidate_id]
        supported = derive_reason_codes(state, candidate, score, tuple(scores), conflicts)
        summaries.append(CandidateSummary(
            candidate_id=candidate.candidate_id,
            operations=tuple(sorted({op.kind.value for op in candidate.operations if op.kind.value in _SAFE_OPERATIONS})),
            sessions=tuple(SessionSummary(
                activity=_activity(session.activity_type),
                weekday=session.local_date.isoweekday(),
                start_period=("morning" if session.schedule_window.start_at.hour < 12 else "afternoon" if session.schedule_window.start_at.hour < 17 else "evening"),
                duration_band=_band(session.duration_minutes),
                intensity=session.intensity.value,
            ) for session in candidate.sessions),
            score=str(score.total),
            goal_alignment=score.goal_alignment,
            preference_adherence=score.preference_adherence,
            schedule_fit=score.schedule_fit,
            recovery_fit=score.recovery_fit,
            training_continuity=score.training_continuity,
            disruption_cost=score.disruption_cost,
            soft_constraint_penalty=score.soft_constraint_penalty,
            supported_reason_codes=tuple(ReasonCode(code) for code in supported if code in ReasonCode._value2member_map_),
        ))
    event_context = tuple(sorted((
        ContextSummary(
            kind=event.kind.lower() if event.kind.lower() in _SAFE_EVENT_KINDS else "other",
            weekday=event.starts_at.astimezone(ZoneInfo(event.timezone)).isoweekday(),
            duration_band=_band((event.ends_at - event.starts_at).total_seconds() / 3600 * 60),
        ) for event in state.life_events
    ), key=lambda item: (item.weekday, item.kind, item.duration_band)))
    safe_constraints = tuple(sorted((
        ConstraintSummary(code=item.code, strength=item.kind.value)
        for item in constraints if item.code in _SAFE_CONSTRAINTS
    ), key=lambda item: (item.code, item.strength)))
    safe_conflicts = tuple(sorted((
        ConflictSummary(code=item.code, severity=item.severity, affected_count=len(item.affected_logical_session_ids))
        for item in conflicts if item.code in _SAFE_CONFLICTS
    ), key=lambda item: (item.code, item.severity, item.affected_count)))
    all_codes = tuple(sorted({code for item in summaries for code in item.supported_reason_codes}, key=str))
    return AIContext(
        horizon_days=(state.horizon_end - state.horizon_start).days + 1,
        recovery=RecoverySummary(
            known=normalized.recovery_known,
            fit_band=("low" if normalized.recovery_fit < 0.34 else "moderate" if normalized.recovery_fit < 0.67 else "high"),
        ),
        goal_priorities=tuple(GoalSummary(kind=goal.kind.value, priority=goal.priority) for goal in state.goals),
        preferences=tuple(PreferenceSummary(days=p.days, strength=p.strength.value) for p in state.preferences if p.key == "preferred_training_days" and p.days),
        event_context=event_context,
        constraints=safe_constraints,
        conflicts=safe_conflicts,
        valid_candidates=tuple(summaries),
        allowed_rationale_codes=all_codes,
    )


def should_adjudicate(scores: tuple[CandidatePlanScore, ...], candidates: tuple[CandidatePlan, ...]) -> bool:
    """Follow frozen policy: close top two scores with a real component tradeoff."""
    by_id = {item.candidate_id: item for item in candidates if item.valid}
    ordered = [item for item in scores if item.valid and item.candidate_id in by_id]
    ordered.sort(key=lambda item: deterministic_rank_key(item, by_id[item.candidate_id].content_hash))
    if len(ordered) < 2 or ordered[0].total - ordered[1].total > Decimal("8.000"):
        return False
    left = ordered[0]
    right = ordered[1]
    dimensions = ("goal_alignment", "preference_adherence", "schedule_fit", "recovery_fit", "training_continuity")
    return any(getattr(left, key) > getattr(right, key) for key in dimensions) and any(
        getattr(right, key) > getattr(left, key) for key in dimensions
    )


async def _invoke(provider: LLMProvider, context: AIContext, timeout: float) -> Any:
    return await asyncio.wait_for(provider.generate_structured(context), timeout=timeout)


def adjudicate(
    provider: LLMProvider,
    *,
    timeout: float,
    state: CurrentState,
    normalized: NormalizedState,
    constraints: tuple[Constraint, ...],
    conflicts: tuple[Conflict, ...],
    candidates: tuple[CandidatePlan, ...],
    scores: tuple[CandidatePlanScore, ...],
) -> tuple[PlannerDecision | None, ValidationResult | None, AdjudicationTrace]:
    """Make at most one call, validate the minimal response, else signal fallback."""
    provider_name = getattr(provider, "provider_name", type(provider).__name__)
    model_name = getattr(provider, "model_name", None)
    if not should_adjudicate(scores, candidates):
        return None, None, AdjudicationTrace(provider=provider_name, model_name=model_name, outcome="not_needed")
    ordered_candidates = {item.candidate_id: item for item in candidates if item.valid}
    ordered_scores = [item for item in scores if item.valid and item.candidate_id in ordered_candidates]
    ordered_scores.sort(key=lambda item: deterministic_rank_key(item, ordered_candidates[item.candidate_id].content_hash))
    ordered = [ordered_candidates[item.candidate_id] for item in ordered_scores]
    context = build_ai_context(state, normalized, constraints, conflicts, tuple(ordered), tuple(ordered_scores))
    started = perf_counter()

    def trace(outcome: str, *, fallback_code: str | None = None) -> AdjudicationTrace:
        return AdjudicationTrace(
            provider=provider_name,
            model_name=model_name,
            latency_ms=round((perf_counter() - started) * 1000),
            attempted=True,
            outcome=outcome,
            fallback_code=fallback_code,
        )

    try:
        raw = asyncio.run(_invoke(provider, context, timeout))
        if isinstance(raw, LLMDecision):
            output = raw
        elif isinstance(raw, str):
            output = LLMDecision.model_validate_json(raw)
        else:
            output = LLMDecision.model_validate(raw)
    except (TimeoutError, asyncio.TimeoutError):
        return None, None, trace("fallback", fallback_code="PROVIDER_TIMEOUT")
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        return None, None, trace("fallback", fallback_code="MALFORMED_OUTPUT")
    except Exception:
        return None, None, trace("fallback", fallback_code="PROVIDER_FAILURE")

    selected = ordered_candidates.get(output.selected_candidate_id)
    if selected is None:
        return None, None, trace("fallback", fallback_code="UNKNOWN_OR_INVALID_CANDIDATE")
    score = next(item for item in ordered_scores if item.candidate_id == selected.candidate_id)
    supported = derive_reason_codes(state, selected, score, tuple(ordered_scores), conflicts)
    if any(code.value not in supported for code in output.reason_codes):
        return None, None, trace("fallback", fallback_code="UNSUPPORTED_REASON_CODE")
    violations = validate_hard_constraints(
        selected.sessions, constraints, state.horizon_start, state.horizon_end, normalized.plan,
    )
    computed_id, computed_hash = _content_identity(selected.sessions)
    if computed_id != selected.candidate_id or computed_hash != selected.content_hash:
        violations += ("CANDIDATE_CONTENT_HASH_MISMATCH",)
    validation = ValidationResult(
        valid=not violations,
        violations=violations,
        candidate_content_hash=selected.content_hash,
    )
    if not validation.valid:
        return None, None, trace("fallback", fallback_code="POST_ADJUDICATION_VALIDATION_FAILED")
    decision = PlannerDecision(
        selected_candidate_id=selected.candidate_id,
        rationale_codes=tuple(code.value for code in output.reason_codes),
    )
    return decision, validation, trace("accepted")
