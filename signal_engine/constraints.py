from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from .models import (
    Conflict,
    Constraint,
    ConstraintKind,
    CurrentState,
    Intensity,
    LifeEvent,
    NormalizedState,
    PlannedSession,
    ScheduleWindow,
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _window_overlap(left: ScheduleWindow, right: ScheduleWindow) -> bool:
    return _utc(left.start_at) < _utc(right.end_at) and _utc(right.start_at) < _utc(left.end_at)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _recovery_fit(state: CurrentState) -> tuple[float, bool]:
    now = _utc(state.as_of)
    observations = [o for o in state.recent_history.physiology if o.quality != "synthetic"]
    terms: list[tuple[float, float]] = []

    sleep = sorted((o for o in observations if o.metric == "sleep_duration" and o.unit == "h" and 0 <= o.value <= 24), key=lambda o: _utc(o.observed_at))
    current_sleep = [o for o in sleep if timedelta(0) <= now - _utc(o.observed_at) <= timedelta(hours=48)]
    baseline_sleep = [o.value for o in sleep if timedelta(0) <= now - _utc(o.observed_at) <= timedelta(days=28)]
    if current_sleep and len(baseline_sleep) >= 7:
        latest = current_sleep[-1].value
        baseline = median(baseline_sleep)
        terms.append((_clip((baseline - latest) / max(baseline, 0.1) / 0.5), 0.50))

    training = [t for t in state.recent_history.training if t.quality != "synthetic" and t.load_value is not None and t.load_value >= 0 and t.load_unit == "session_rpe_load"]
    recent_start = now - timedelta(days=7)
    recent_load = sum(t.load_value or 0 for t in training if recent_start <= _utc(t.occurred_at) <= now)
    week_totals: list[float] = []
    covered_week_ends = {_utc(value) for value in state.recent_history.complete_training_week_ends}
    for offset in range(2, 6):
        end = now - timedelta(days=7 * (offset - 1))
        start = end - timedelta(days=7)
        if end in covered_week_ends:
            week_totals.append(sum(t.load_value or 0 for t in training if start <= _utc(t.occurred_at) < end))
    if len(week_totals) >= 3:
        baseline_load = median(week_totals)
        terms.append((_clip((recent_load - baseline_load) / max(baseline_load, 1)), 0.30))

    soreness = sorted((o for o in observations if o.metric == "soreness" and o.unit in {"0-10", "score_0_10"} and 0 <= o.value <= 10), key=lambda o: _utc(o.observed_at))
    recent_soreness = [o for o in soreness if timedelta(0) <= now - _utc(o.observed_at) <= timedelta(hours=48)]
    if recent_soreness:
        terms.append((_clip(recent_soreness[-1].value / 10), 0.20))

    if not terms:
        return 0.5, False
    weight_sum = sum(weight for _, weight in terms)
    stress = sum(value * weight for value, weight in terms) / weight_sum
    return _clip(1 - stress), True


def normalize_state(state: CurrentState) -> NormalizedState:
    """Normalize temporal facts to UTC while preserving the IANA display zone."""
    plan = tuple(sorted((
        session.model_copy(update={
            "schedule_window": session.schedule_window.model_copy(update={
                "start_at": _utc(session.schedule_window.start_at),
                "end_at": _utc(session.schedule_window.end_at),
            })
        }) for session in state.current_plan
    ), key=lambda s: (s.schedule_window.start_at, s.logical_session_id)))
    events = tuple(sorted((
        event.model_copy(update={"starts_at": _utc(event.starts_at), "ends_at": _utc(event.ends_at)})
        for event in state.life_events
    ), key=lambda e: (e.starts_at, e.kind)))
    recovery_windows: list[ScheduleWindow] = []
    for event in events:
        zone = ZoneInfo(event.timezone)
        local_end = event.ends_at.astimezone(zone)
        if time(4, 0) <= local_end.timetz().replace(tzinfo=None) <= time(9, 0):
            local_noon = datetime.combine(local_end.date(), time(12, 0), tzinfo=zone)
            local_limit = min(local_end + timedelta(hours=6), local_noon)
            if local_limit > local_end:
                recovery_windows.append(ScheduleWindow(
                    start_at=local_end,
                    end_at=local_limit,
                    timezone=event.timezone,
                ))
    recovery_fit, recovery_known = _recovery_fit(state)
    return NormalizedState(
        source=state,
        plan=plan,
        events=events,
        recovery_fit=recovery_fit,
        recovery_known=recovery_known,
        recovery_windows=tuple(sorted(recovery_windows, key=lambda w: w.start_at)),
    )


def detect_constraints(state: NormalizedState) -> tuple[Constraint, ...]:
    constraints: list[Constraint] = []
    for index, event in enumerate(state.events):
        constraints.append(Constraint(
            code="EVENT_UNAVAILABLE",
            kind=ConstraintKind.HARD,
            window=ScheduleWindow(start_at=event.starts_at, end_at=event.ends_at, timezone=event.timezone),
            source_type="life_event",
            parameters={"event_kind": event.kind, "ordinal": index},
        ))
    for window in state.recovery_windows:
        constraints.append(Constraint(
            code="REDUCED_RECOVERY_WINDOW",
            kind=ConstraintKind.SOFT,
            window=window,
            source_type="derived_context",
        ))
    for logical_id in sorted(state.source.must_preserve_session_ids):
        constraints.append(Constraint(
            code="PRESERVE_SESSION",
            kind=ConstraintKind.HARD,
            target_logical_session_id=logical_id,
            source_type="user_instruction",
        ))
    lower_body = [s for s in state.plan if s.activity_type.lower() in {"lower", "lower_body"}]
    if len(lower_body) > 1:
        constraints.append(Constraint(
            code="LOWER_BODY_SPACING",
            kind=ConstraintKind.HARD,
            source_type="policy",
            parameters={"minimum_calendar_day_gap": 2},
        ))
    for preference in state.source.preferences:
        if preference.days:
            constraints.append(Constraint(
                code="PREFERRED_TRAINING_DAYS",
                kind=preference.strength,
                source_type="preference",
                parameters={"days": preference.days, "preference_key": preference.key},
            ))
    return tuple(constraints)


def detect_conflicts(
    state: NormalizedState,
    constraints: tuple[Constraint, ...],
) -> tuple[Conflict, ...]:
    found: list[Conflict] = []
    for constraint in constraints:
        affected: list[str] = []
        for session in state.plan:
            if constraint.window is not None and _window_overlap(session.schedule_window, constraint.window):
                if constraint.code == "REDUCED_RECOVERY_WINDOW" and session.intensity in {Intensity.RECOVERY, Intensity.REST, Intensity.EASY}:
                    continue
                affected.append(session.logical_session_id)
            elif constraint.code == "PREFERRED_TRAINING_DAYS" and session.intensity not in {Intensity.REST, Intensity.RECOVERY}:
                allowed = set(constraint.parameters["days"])
                if session.local_date.isoweekday() not in allowed:
                    affected.append(session.logical_session_id)
        if constraint.code == "LOWER_BODY_SPACING":
            lower = sorted((s for s in state.plan if s.activity_type.lower() in {"lower", "lower_body"}), key=lambda s: (s.local_date, s.logical_session_id))
            for left, right in zip(lower, lower[1:]):
                if (right.local_date - left.local_date).days < constraint.parameters["minimum_calendar_day_gap"]:
                    affected.extend((left.logical_session_id, right.logical_session_id))
        if affected:
            found.append(Conflict(
                code="RECOVERY_CONFLICT" if constraint.code == "REDUCED_RECOVERY_WINDOW" else constraint.code,
                severity="high" if constraint.kind == ConstraintKind.HARD else "medium",
                affected_logical_session_ids=tuple(sorted(set(affected))),
                constraint_codes=(constraint.code,),
            ))
    ordered = sorted(state.plan, key=lambda s: (s.schedule_window.start_at, s.logical_session_id))
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1:]:
            if _window_overlap(left.schedule_window, right.schedule_window):
                found.append(Conflict(
                    code="SCHEDULE_OVERLAP",
                    severity="blocking",
                    affected_logical_session_ids=tuple(sorted((left.logical_session_id, right.logical_session_id))),
                    constraint_codes=("SESSION_CAPACITY",),
                ))
    return tuple(found)


def validate_hard_constraints(
    sessions: tuple[PlannedSession, ...],
    constraints: tuple[Constraint, ...],
    horizon_start,
    horizon_end,
    original_sessions: tuple[PlannedSession, ...] = (),
) -> tuple[str, ...]:
    violations: list[str] = []
    ids = [s.logical_session_id for s in sessions]
    if len(ids) != len(set(ids)):
        violations.append("DUPLICATE_LOGICAL_SESSION")
    for session in sessions:
        if not horizon_start <= session.local_date <= horizon_end:
            violations.append(f"OUTSIDE_HORIZON:{session.logical_session_id}")
    ordered = sorted(sessions, key=lambda s: (s.schedule_window.start_at, s.logical_session_id))
    for left, right in zip(ordered, ordered[1:]):
        if _window_overlap(left.schedule_window, right.schedule_window):
            violations.append(f"SESSION_OVERLAP:{left.logical_session_id}:{right.logical_session_id}")
    for constraint in constraints:
        if constraint.kind != ConstraintKind.HARD:
            continue
        if constraint.window is not None:
            for session in sessions:
                if _window_overlap(session.schedule_window, constraint.window):
                    violations.append(f"{constraint.code}:{session.logical_session_id}")
        if constraint.code == "PRESERVE_SESSION":
            after = next((s for s in sessions if s.logical_session_id == constraint.target_logical_session_id), None)
            original = next((s for s in original_sessions if s.logical_session_id == constraint.target_logical_session_id), None)
            if after is None or original is None:
                violations.append(f"MISSING_PRESERVED_SESSION:{constraint.target_logical_session_id}")
            elif after != original:
                violations.append(f"MODIFIED_PRESERVED_SESSION:{constraint.target_logical_session_id}")
        if constraint.code == "PREFERRED_TRAINING_DAYS":
            allowed = set(constraint.parameters["days"])
            for session in sessions:
                if session.intensity not in {Intensity.REST, Intensity.RECOVERY} and session.local_date.isoweekday() not in allowed:
                    violations.append(f"PREFERRED_DAY:{session.logical_session_id}")
        if constraint.code == "LOWER_BODY_SPACING":
            lower = sorted((s for s in sessions if s.activity_type.lower() in {"lower", "lower_body"}), key=lambda s: (s.local_date, s.logical_session_id))
            minimum_gap = constraint.parameters["minimum_calendar_day_gap"]
            if any((right.local_date - left.local_date).days < minimum_gap for left, right in zip(lower, lower[1:])):
                violations.append("LOWER_BODY_SPACING")
    return tuple(violations)
