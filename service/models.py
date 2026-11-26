from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from signal_engine.models import (
    CandidatePlan, CandidatePlanScore, Conflict, CurrentState, Goal, LifeEvent,
    PlannedSession, Preference, RecentHistory, ScheduleWindow, ValidationResult,
)

class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AdjudicationTrace(FrozenModel):
    provider: str = "none"
    model_name: str | None = None
    latency_ms: int | None = None
    attempted: bool = False
    outcome: str = "deterministic"
    fallback_code: str | None = None

    @computed_field
    @property
    def adjudication_attempted(self) -> bool:
        return self.attempted

    @computed_field
    @property
    def adjudication_used(self) -> bool:
        return self.outcome == "accepted"

    @computed_field
    @property
    def fallback_used(self) -> bool:
        return self.outcome == "fallback"


class ReplanStatus(StrEnum):
    PROPOSED = "proposed"
    INVALID_INPUT = "invalid_input"
    INVALID_SCHEDULE_WINDOW = "invalid_schedule_window"
    NO_CURRENT_PLAN = "no_current_plan"
    INCONSISTENT_STATE = "inconsistent_state"
    NO_VALID_CANDIDATE = "no_valid_candidate"
    ENGINE_VALIDATION_FAILED = "engine_validation_failed"


class ApplicationState(FrozenModel):
    """Application facts needed to construct a domain CurrentState."""

    as_of: datetime
    timezone: str
    horizon_start: date
    horizon_end: date
    recent_history: RecentHistory = Field(default_factory=RecentHistory)
    available_windows: tuple[ScheduleWindow, ...] = ()
    must_preserve_session_ids: tuple[str, ...] = ()

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @field_validator("timezone")
    @classmethod
    def require_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA identifier") from exc
        return value

    @model_validator(mode="after")
    def valid_horizon(self) -> "ApplicationState":
        if self.horizon_end < self.horizon_start:
            raise ValueError("horizon_end must not precede horizon_start")
        return self


class ReplanRequest(FrozenModel):
    current_plan: tuple[PlannedSession, ...] | None = None
    current_state: ApplicationState
    goals: tuple[Goal, ...] = ()
    preferences: tuple[Preference, ...] = ()
    life_event: LifeEvent | None = None

    def to_domain(self) -> CurrentState:
        """Create the engine input without adding planning policy."""
        if self.current_plan is None:
            raise ValueError("current plan is required for replanning")
        state = self.current_state
        return CurrentState(
            as_of=state.as_of,
            timezone=state.timezone,
            horizon_start=state.horizon_start,
            horizon_end=state.horizon_end,
            current_plan=self.current_plan,
            goals=self.goals,
            preferences=self.preferences,
            life_events=(self.life_event,) if self.life_event is not None else (),
            recent_history=state.recent_history,
            available_windows=state.available_windows,
            must_preserve_session_ids=state.must_preserve_session_ids,
        )


class PlanChange(FrozenModel):
    logical_session_id: str
    change_type: str
    before: PlannedSession | None = None
    after: PlannedSession | None = None


class DecisionExplanation(FrozenModel):
    """Structured facts for a future deterministic explanation renderer."""

    reason_codes: tuple[str, ...] = ()
    changes: tuple[PlanChange, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    selected_score: CandidatePlanScore | None = None


class DecisionInputSnapshot(FrozenModel):
    """Minimized, typed decision inputs suitable for later audit persistence."""

    as_of: datetime
    timezone: str
    horizon_start: date
    horizon_end: date
    original_plan: tuple[PlannedSession, ...]
    goals: tuple[Goal, ...]
    preferences: tuple[Preference, ...]
    life_event: LifeEvent | None = None
    recent_history: RecentHistory = Field(default_factory=RecentHistory)
    available_windows: tuple[ScheduleWindow, ...] = ()
    must_preserve_session_ids: tuple[str, ...] = ()


class ReplanResult(FrozenModel):
    """Application-facing proposal; IDs remain unset until persistence exists."""

    decision_id: str | None = None
    status: ReplanStatus
    original_plan: tuple[PlannedSession, ...] = ()
    proposed_plan: tuple[PlannedSession, ...] | None = None
    selected_candidate: CandidatePlan | None = None
    candidate_plans: tuple[CandidatePlan, ...] = ()
    candidate_scores: tuple[CandidatePlanScore, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    changes: tuple[PlanChange, ...] = ()
    reason_codes: tuple[str, ...] = ()
    validation: ValidationResult
    explanation: DecisionExplanation
    adjudication: AdjudicationTrace = Field(default_factory=AdjudicationTrace, exclude=True)
    input_snapshot: DecisionInputSnapshot | None = None
    engine_version: str = "deterministic-mvp-v1"
    policy_version: str = "mvp-v1"
    validation_errors: tuple[str, ...] = ()


def empty_result(status: ReplanStatus, *, validation: ValidationResult, errors: tuple[str, ...] = ()) -> ReplanResult:
    return ReplanResult(
        status=status,
        validation=validation,
        explanation=DecisionExplanation(),
        validation_errors=errors,
    )


def build_changes(before: tuple[PlannedSession, ...], after: tuple[PlannedSession, ...]) -> tuple[PlanChange, ...]:
    old = {session.logical_session_id: session for session in before}
    new = {session.logical_session_id: session for session in after}
    changes: list[PlanChange] = []
    for logical_id in sorted(old.keys() | new.keys()):
        previous, current = old.get(logical_id), new.get(logical_id)
        if previous == current:
            continue
        if previous is None:
            kind = "added"
        elif current is None:
            kind = "removed"
        elif previous.schedule_window != current.schedule_window:
            kind = "moved"
        else:
            kind = "modified"
        changes.append(PlanChange(logical_session_id=logical_id, change_type=kind, before=previous, after=current))
    return tuple(changes)


def error_result(status: ReplanStatus, errors: tuple[str, ...]) -> ReplanResult:
    return empty_result(
        status,
        validation=ValidationResult(valid=False, violations=(status.value.upper(),)),
        errors=errors,
    )
