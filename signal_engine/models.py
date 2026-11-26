from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConstraintKind(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class GoalKind(StrEnum):
    STRENGTH = "STRENGTH"
    RUNNING = "RUNNING"
    ENDURANCE = "ENDURANCE"
    RECOVERY = "RECOVERY"
    GENERAL_FITNESS = "GENERAL_FITNESS"


class Intensity(StrEnum):
    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"
    RECOVERY = "recovery"
    REST = "rest"


class OperationKind(StrEnum):
    MOVE_SESSION = "MOVE_SESSION"
    REMOVE_SESSION = "REMOVE_SESSION"
    ADD_RECOVERY = "ADD_RECOVERY"
    REDUCE_INTENSITY = "REDUCE_INTENSITY"
    REDUCE_DURATION = "REDUCE_DURATION"
    SWAP_SESSIONS = "SWAP_SESSIONS"
    PRESERVE_SESSION = "PRESERVE_SESSION"
    NO_CHANGE = "NO_CHANGE"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _iana(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA identifier") from exc
    return value


class ScheduleWindow(FrozenModel):
    start_at: datetime
    end_at: datetime
    timezone: str

    _aware_start = field_validator("start_at")(_aware)
    _aware_end = field_validator("end_at")(_aware)
    _iana_timezone = field_validator("timezone")(_iana)

    @model_validator(mode="after")
    def positive_window(self) -> "ScheduleWindow":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be strictly after start_at")
        return self

    @property
    def local_start_date(self) -> date:
        return self.start_at.astimezone(ZoneInfo(self.timezone)).date()


class PlannedSession(FrozenModel):
    logical_session_id: str = Field(min_length=1)
    activity_type: str = Field(min_length=1)
    schedule_window: ScheduleWindow
    intensity: Intensity
    duration_minutes: int = Field(gt=0, le=600)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def local_date(self) -> date:
        return self.schedule_window.local_start_date


class Goal(FrozenModel):
    kind: GoalKind
    priority: int = Field(ge=1, le=5)
    target_value: float | None = Field(default=None, gt=0)
    target_unit: str | None = None

    @model_validator(mode="after")
    def valid_target_unit(self) -> "Goal":
        supported = {
            GoalKind.STRENGTH: {"sessions/week"},
            GoalKind.RUNNING: {"km/week", "sessions/week"},
            GoalKind.ENDURANCE: {"minutes/week", "sessions/week"},
            GoalKind.RECOVERY: {"days/week"},
            GoalKind.GENERAL_FITNESS: {"sessions/week"},
        }
        if self.target_unit is not None and self.target_unit not in supported[self.kind]:
            raise ValueError(f"unsupported target unit for {self.kind.value}: {self.target_unit}")
        return self


class Preference(FrozenModel):
    """Small MVP preference contract: allowed local weekdays, hard or soft."""

    key: str = "preferred_training_days"
    days: tuple[int, ...] = ()  # ISO weekday numbers, Monday=1.
    strength: ConstraintKind = ConstraintKind.SOFT

    @field_validator("days")
    @classmethod
    def valid_days(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("weekday values must be ISO weekdays 1..7")
        return tuple(sorted(set(value)))


class LifeEvent(FrozenModel):
    kind: str
    starts_at: datetime
    ends_at: datetime
    timezone: str

    _aware_start = field_validator("starts_at")(_aware)
    _aware_end = field_validator("ends_at")(_aware)
    _iana_timezone = field_validator("timezone")(_iana)

    @model_validator(mode="after")
    def positive_window(self) -> "LifeEvent":
        if self.ends_at <= self.starts_at:
            raise ValueError("life event ends_at must be after starts_at")
        return self


class PhysiologyObservation(FrozenModel):
    metric: str
    value: float
    unit: str
    observed_at: datetime
    quality: str = "self_reported"

    _aware_observed = field_validator("observed_at")(_aware)


class TrainingOutcome(FrozenModel):
    occurred_at: datetime
    load_value: float | None = None
    load_unit: str | None = None
    quality: Literal["self_reported", "measured", "synthetic"] = "self_reported"

    _aware_occurred = field_validator("occurred_at")(_aware)


class RecentHistory(FrozenModel):
    physiology: tuple[PhysiologyObservation, ...] = ()
    training: tuple[TrainingOutcome, ...] = ()
    # A data-ingestion layer must assert coverage before a baseline week is
    # treated as complete; the planner never infers coverage from a workout row.
    complete_training_week_ends: tuple[datetime, ...] = ()

    @field_validator("complete_training_week_ends")
    @classmethod
    def aware_week_ends(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        return tuple(_aware(value) for value in values)


class CurrentState(FrozenModel):
    as_of: datetime
    timezone: str
    horizon_start: date
    horizon_end: date
    current_plan: tuple[PlannedSession, ...]
    goals: tuple[Goal, ...] = ()
    preferences: tuple[Preference, ...] = ()
    life_events: tuple[LifeEvent, ...] = ()
    recent_history: RecentHistory = Field(default_factory=RecentHistory)
    available_windows: tuple[ScheduleWindow, ...] = ()
    must_preserve_session_ids: tuple[str, ...] = ()

    _aware_as_of = field_validator("as_of")(_aware)
    _iana_timezone = field_validator("timezone")(_iana)

    @model_validator(mode="after")
    def valid_horizon(self) -> "CurrentState":
        if self.horizon_end < self.horizon_start:
            raise ValueError("horizon_end must not precede horizon_start")
        if len({s.logical_session_id for s in self.current_plan}) != len(self.current_plan):
            raise ValueError("current plan has duplicate logical session IDs")
        return self


class NormalizedState(FrozenModel):
    source: CurrentState
    plan: tuple[PlannedSession, ...]
    events: tuple[LifeEvent, ...]
    recovery_fit: float = Field(ge=0, le=1)
    recovery_known: bool
    recovery_windows: tuple[ScheduleWindow, ...] = ()


class Constraint(FrozenModel):
    code: str
    kind: ConstraintKind
    window: ScheduleWindow | None = None
    target_logical_session_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    source_type: str


class Conflict(FrozenModel):
    code: str
    severity: str
    affected_logical_session_ids: tuple[str, ...]
    constraint_codes: tuple[str, ...]


class CandidateOperation(FrozenModel):
    kind: OperationKind
    logical_session_ids: tuple[str, ...] = ()
    detail: str = ""


class CandidatePlan(FrozenModel):
    candidate_id: str
    content_hash: str
    sessions: tuple[PlannedSession, ...]
    operations: tuple[CandidateOperation, ...]
    valid: bool = True
    reason_codes: tuple[str, ...] = ()


class CandidatePlanScore(FrozenModel):
    candidate_id: str
    policy_version: str = "mvp-v1"
    valid: bool
    goal_alignment: float = Field(ge=0, le=1)
    preference_adherence: float = Field(ge=0, le=1)
    schedule_fit: float = Field(ge=0, le=1)
    recovery_fit: float = Field(ge=0, le=1)
    training_continuity: float = Field(ge=0, le=1)
    disruption_cost: float = Field(ge=0, le=100)
    soft_constraint_penalty: float = Field(ge=0, le=100)
    total: Decimal
    reason_codes: tuple[str, ...] = ()


class PlannerDecision(FrozenModel):
    selected_candidate_id: str
    rationale_codes: tuple[str, ...] = ()


class ValidationResult(FrozenModel):
    valid: bool
    violations: tuple[str, ...] = ()
    validator_version: str = "mvp-v1"
    candidate_content_hash: str | None = None


class PipelineResult(FrozenModel):
    normalized_state: NormalizedState
    constraints: tuple[Constraint, ...]
    conflicts: tuple[Conflict, ...]
    candidates: tuple[CandidatePlan, ...]
    scores: tuple[CandidatePlanScore, ...]
    decision: PlannerDecision | None
    validation: ValidationResult


class NoValidCandidateError(ValueError):
    """Raised only by convenience selection when no feasible candidate exists."""
