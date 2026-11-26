"""MVP scoring assumptions: goal targets are doses over the supplied horizon;
when absent, parent-plan dose is the target. The current slice has no separate
preferred time-of-day windows, so schedule_fit is neutral 1.0. Activity-to-goal
mappings below are a deliberately small catalog, not inferred from free text.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from math import prod
from types import MappingProxyType

from .models import (
    CandidatePlan,
    CandidatePlanScore,
    Constraint,
    ConstraintKind,
    CurrentState,
    Goal,
    GoalKind,
    Intensity,
    NormalizedState,
    PlannedSession,
)


WEIGHTS = MappingProxyType({
    "goal_alignment": 0.30,
    "preference_adherence": 0.15,
    "schedule_fit": 0.15,
    "recovery_fit": 0.20,
    "training_continuity": 0.20,
})


def _is_training(session: PlannedSession) -> bool:
    return session.intensity not in {Intensity.REST, Intensity.RECOVERY} and session.activity_type.lower() != "recovery"


def _goal_matches(kind: GoalKind, session: PlannedSession) -> bool:
    activity = session.activity_type.lower()
    if kind == GoalKind.STRENGTH:
        return activity in {"upper", "lower", "lower_body", "strength"}
    if kind in {GoalKind.RUNNING, GoalKind.ENDURANCE}:
        return activity in {"run", "long_run", "running", "endurance"}
    if kind == GoalKind.RECOVERY:
        return session.intensity in {Intensity.RECOVERY, Intensity.REST} or activity == "recovery"
    return _is_training(session)


def _dose(sessions: tuple[PlannedSession, ...], kind: GoalKind, unit: str | None = None) -> float:
    matching = [s for s in sessions if _goal_matches(kind, s)]
    if unit == "km/week":
        distances = [s.attributes.get("distance_km") for s in matching]
        if any(value is not None for value in distances):
            return sum(float(value or 0) for value in distances)
        # The frozen dose rule falls back to running session-equivalents when
        # canonical distance measurements are not present.
        return sum(0.75 if s.intensity == Intensity.EASY else 1.0 for s in matching)
    if unit == "minutes/week":
        return sum(float(s.duration_minutes) for s in matching)
    if unit == "days/week":
        return float(len({s.local_date for s in matching}))
    return sum(0.75 if s.intensity == Intensity.EASY else 1.0 for s in matching)


def _goal_alignment(candidate: tuple[PlannedSession, ...], state: CurrentState) -> float:
    if not state.goals:
        return 1.0
    weighted = 0.0
    total_weight = 0
    for goal in state.goals:
        parent_dose = _dose(state.current_plan, goal.kind, goal.target_unit)
        target = goal.target_value if goal.target_value is not None else parent_dose
        ratio = 1.0 if not target else min(1.0, _dose(candidate, goal.kind, goal.target_unit) / target)
        weighted += ratio * goal.priority
        total_weight += goal.priority
    return weighted / total_weight if total_weight else 1.0


def _preference_adherence(candidate: tuple[PlannedSession, ...], state: CurrentState) -> float:
    preferences = [p for p in state.preferences if p.days]
    if not preferences:
        return 1.0
    sessions = [s for s in candidate if _is_training(s)]
    if not sessions:
        return 1.0
    values = [sum(s.local_date.isoweekday() in p.days for s in sessions) / len(sessions) for p in preferences]
    return sum(values) / len(values)


def _schedule_fit(candidate: tuple[PlannedSession, ...], constraints: tuple[Constraint, ...]) -> float:
    """Return normalized schedule fit; hard windows are feasibility, not fit.

    The current domain contract models preferred training weekdays, but no
    preferred time windows. Weekday preferences are evaluated by preference
    adherence, so a feasible schedule has neutral-perfect schedule fit here.
    """
    soft_windows = [
        constraint.window
        for constraint in constraints
        if constraint.kind.value == "soft" and constraint.code == "PREFERRED_TIME_WINDOW" and constraint.window
    ]
    training = [session for session in candidate if _is_training(session)]
    if not soft_windows or not training:
        return 1.0
    outside = sum(
        not any(
            window.start_at <= session.schedule_window.start_at
            and session.schedule_window.end_at <= window.end_at
            for window in soft_windows
        )
        for session in training
    )
    return 1.0 - outside / len(training)


def _continuity(candidate: tuple[PlannedSession, ...], parent: tuple[PlannedSession, ...]) -> float:
    parent_dose = sum(0.75 if s.intensity == Intensity.EASY else 1.0 for s in parent if _is_training(s))
    if parent_dose == 0:
        return 1.0
    candidate_dose = sum(0.75 if s.intensity == Intensity.EASY else 1.0 for s in candidate if _is_training(s))
    return min(1.0, candidate_dose / parent_dose)


def _disruption_cost(candidate: tuple[PlannedSession, ...], parent: tuple[PlannedSession, ...]) -> float:
    before = {s.logical_session_id: s for s in parent}
    after = {s.logical_session_id: s for s in candidate}
    cost = 0.0
    touched_dates: set[date] = set()
    for logical_id in sorted(before.keys() | after.keys()):
        old, new = before.get(logical_id), after.get(logical_id)
        if old is None:
            cost += 5
        elif new is None:
            cost += 15
        else:
            if old.schedule_window != new.schedule_window:
                cost += 4
                touched_dates.update((old.local_date, new.local_date))
            if old.intensity != new.intensity:
                cost += 5
            if new.duration_minutes < old.duration_minutes:
                cost += min(6, 6 * (old.duration_minutes - new.duration_minutes) / old.duration_minutes)
    cost += 2 * len(touched_dates)
    return min(100.0, cost)


def _soft_penalty(candidate: tuple[PlannedSession, ...], constraints: tuple[Constraint, ...]) -> float:
    penalty = 0.0
    for constraint in constraints:
        if constraint.kind == ConstraintKind.HARD:
            continue
        if constraint.code == "REDUCED_RECOVERY_WINDOW" and constraint.window:
            if any(
                s.intensity in {Intensity.MODERATE, Intensity.HARD}
                and s.schedule_window.start_at < constraint.window.end_at
                and constraint.window.start_at < s.schedule_window.end_at
                for s in candidate
            ):
                penalty += 25
        elif constraint.code == "PREFERRED_TRAINING_DAYS":
            allowed = set(constraint.parameters["days"])
            missed = [s for s in candidate if _is_training(s) and s.local_date.isoweekday() not in allowed]
            if missed:
                penalty += 5  # MVP default within the specified 0–10 soft-preference range.
    return min(100.0, penalty)


def score_candidate(
    candidate: CandidatePlan,
    normalized: NormalizedState,
    constraints: tuple[Constraint, ...],
) -> CandidatePlanScore:
    sessions = candidate.sessions
    state = normalized.source
    goal = _goal_alignment(sessions, state)
    preference = _preference_adherence(sessions, state)
    schedule = _schedule_fit(sessions, constraints)
    recovery = normalized.recovery_fit
    continuity = _continuity(sessions, normalized.plan)
    benefit = 100 * prod((
        goal ** WEIGHTS["goal_alignment"],
        preference ** WEIGHTS["preference_adherence"],
        schedule ** WEIGHTS["schedule_fit"],
        recovery ** WEIGHTS["recovery_fit"],
        continuity ** WEIGHTS["training_continuity"],
    ))
    disruption = _disruption_cost(sessions, normalized.plan)
    soft_penalty = _soft_penalty(sessions, constraints)
    total = (Decimal(str(benefit - disruption - soft_penalty))).quantize(Decimal("0.001"), rounding=ROUND_HALF_EVEN)
    return CandidatePlanScore(
        candidate_id=candidate.candidate_id,
        valid=candidate.valid,
        goal_alignment=goal,
        preference_adherence=preference,
        schedule_fit=schedule,
        recovery_fit=recovery,
        training_continuity=continuity,
        disruption_cost=disruption,
        soft_constraint_penalty=soft_penalty,
        total=total,
        reason_codes=candidate.reason_codes,
    )


def score_candidates(
    candidates: tuple[CandidatePlan, ...],
    normalized: NormalizedState,
    constraints: tuple[Constraint, ...],
) -> tuple[CandidatePlanScore, ...]:
    return tuple(sorted(
        (score_candidate(candidate, normalized, constraints) for candidate in candidates if candidate.valid),
        key=lambda score: score.candidate_id,
    ))


def deterministic_rank_key(score: CandidatePlanScore, content_hash: str) -> tuple:
    """Exact documented tuple: total, goal, recovery, lower disruption, hash."""
    return (
        -score.total,
        -score.goal_alignment,
        -score.recovery_fit,
        score.disruption_cost,
        content_hash,
    )
