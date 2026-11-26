from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from signal_engine.candidates import generate_candidates
from signal_engine.constraints import (
    detect_conflicts,
    detect_constraints,
    normalize_state,
    validate_hard_constraints,
)
from signal_engine.engine import run_planner
from signal_engine.models import (
    CandidatePlan,
    CandidatePlanScore,
    Constraint,
    ConstraintKind,
    CurrentState,
    Goal,
    GoalKind,
    Intensity,
    LifeEvent,
    PlannedSession,
    Preference,
    ScheduleWindow,
)
from signal_engine.scoring import (
    _continuity,
    _disruption_cost,
    _goal_alignment,
    _preference_adherence,
    _schedule_fit,
    deterministic_rank_key,
    score_candidate,
)


ZONE = "Asia/Calcutta"
MONDAY = date(2026, 9, 28)


def window(day: int, start_hour: int, end_hour: int, *, month: int = 9) -> ScheduleWindow:
    # Canonical scenario is a single week spanning September into October.
    year = 2026
    start = datetime(year, month, day, start_hour, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    end = datetime(year, month, day, end_hour, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return ScheduleWindow(start_at=start, end_at=end, timezone=ZONE)


def session(logical_id: str, activity: str, day: int, start: int, end: int, intensity=Intensity.MODERATE) -> PlannedSession:
    return PlannedSession(
        logical_session_id=logical_id,
        activity_type=activity,
        schedule_window=window(day, start, end, month=10 if day == 2 else 9),
        intensity=intensity,
        duration_minutes=(end - start) * 60,
    )


def canonical_state(**updates) -> CurrentState:
    plan = (
        session("upper-01", "upper", 28, 18, 19),
        PlannedSession(
            logical_session_id="run-01", activity_type="run",
            schedule_window=ScheduleWindow(start_at=datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc), end_at=datetime(2026, 9, 29, 13, 15, tzinfo=timezone.utc), timezone=ZONE),
            intensity=Intensity.MODERATE, duration_minutes=45,
        ),
        session("lower-01", "lower_body", 30, 18, 19),
        PlannedSession(
            logical_session_id="run-02", activity_type="run",
            schedule_window=ScheduleWindow(start_at=datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc), end_at=datetime(2026, 10, 2, 13, 15, tzinfo=timezone.utc), timezone=ZONE),
            intensity=Intensity.MODERATE, duration_minutes=45,
        ),
        PlannedSession(
            logical_session_id="lower-02", activity_type="lower_body",
            schedule_window=ScheduleWindow(start_at=datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc), end_at=datetime(2026, 10, 3, 2, 30, tzinfo=timezone.utc), timezone=ZONE),
            intensity=Intensity.MODERATE, duration_minutes=60,
        ),
        PlannedSession(
            logical_session_id="run-long-01", activity_type="long_run",
            schedule_window=ScheduleWindow(start_at=datetime(2026, 10, 4, 1, 30, tzinfo=timezone.utc), end_at=datetime(2026, 10, 4, 3, 30, tzinfo=timezone.utc), timezone=ZONE),
            intensity=Intensity.MODERATE, duration_minutes=120,
        ),
    )
    base = CurrentState(
        as_of=datetime(2026, 9, 28, 12, tzinfo=timezone.utc),
        timezone=ZONE,
        horizon_start=date(2026, 9, 28),
        horizon_end=date(2026, 10, 4),
        current_plan=plan,
        goals=(Goal(kind=GoalKind.STRENGTH, priority=5), Goal(kind=GoalKind.RUNNING, priority=3)),
        life_events=(LifeEvent(
            kind="social",
            starts_at=datetime(2026, 10, 2, 17, 30, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc),
            timezone=ZONE,
        ),),
        available_windows=(window(28, 18, 19), window(1, 18, 19, month=10)),
        must_preserve_session_ids=("run-long-01",),
    )
    return base.model_copy(update=updates)


def test_schedule_overlap_detection_uses_absolute_instants():
    first = ScheduleWindow(start_at=datetime(2026, 1, 1, 10, tzinfo=timezone.utc), end_at=datetime(2026, 1, 1, 11, tzinfo=timezone.utc), timezone="UTC")
    same_instant_overlap = ScheduleWindow(start_at=datetime(2026, 1, 1, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))), end_at=datetime(2026, 1, 1, 16, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))), timezone=ZONE)
    state = canonical_state(life_events=())
    normalized = normalize_state(state)
    plan = (
        PlannedSession(logical_session_id="a", activity_type="upper", schedule_window=first, intensity=Intensity.MODERATE, duration_minutes=60),
        PlannedSession(logical_session_id="b", activity_type="run", schedule_window=same_instant_overlap, intensity=Intensity.MODERATE, duration_minutes=60),
    )
    conflicted = normalized.model_copy(update={"plan": plan})
    assert any(c.code == "SCHEDULE_OVERLAP" for c in detect_conflicts(conflicted, ()))


def test_hard_constraint_invalidates_candidate():
    state = canonical_state()
    normalized = normalize_state(state)
    constraints = detect_constraints(normalized)
    event_constraint = next(c for c in constraints if c.code == "EVENT_UNAVAILABLE")
    violations = validate_hard_constraints(
        (state.current_plan[0].model_copy(update={"schedule_window": event_constraint.window}),),
        (event_constraint,), state.horizon_start, state.horizon_end,
        original_sessions=state.current_plan,
    )
    assert violations and any(v.startswith("EVENT_UNAVAILABLE") for v in violations)


def test_soft_constraint_penalty_is_scored_without_invalidation():
    result = run_planner(canonical_state())
    unchanged = next(c for c in result.candidates if {o.kind.value for o in c.operations}.issuperset({"NO_CHANGE"}))
    score = next(s for s in result.scores if s.candidate_id == unchanged.candidate_id)
    assert unchanged.valid
    assert score.soft_constraint_penalty == 25


def test_candidate_generation_returns_only_hard_legal_candidates():
    state = canonical_state()
    normalized = normalize_state(state)
    constraints = detect_constraints(normalized)
    candidates = generate_candidates(normalized, constraints)
    assert len(candidates) > 1
    assert all(not validate_hard_constraints(c.sessions, constraints, state.horizon_start, state.horizon_end, normalized.plan) for c in candidates)


def test_scoring_components_are_normalized():
    result = run_planner(canonical_state())
    for score in result.scores:
        for component in (score.goal_alignment, score.preference_adherence, score.schedule_fit, score.recovery_fit, score.training_continuity):
            assert 0 <= component <= 1


def test_each_scoring_component_is_separately_evaluable():
    state = canonical_state()
    normalized = normalize_state(state)
    constraints = detect_constraints(normalized)
    plan = normalized.plan
    assert _goal_alignment(plan, state) == 1
    assert _preference_adherence(plan, state) == 1
    assert _schedule_fit(plan, constraints) == 1
    assert normalized.recovery_fit == 0.5  # Missing evidence is neutral, not zero.
    assert _continuity(plan, plan) == 1


def test_intensity_and_duration_reduction_operations_are_generated_when_relevant():
    state = canonical_state(life_events=(), preferences=(Preference(days=(1,)),))
    normalized = normalize_state(state)
    constraints = detect_constraints(normalized)
    candidates = generate_candidates(normalized, constraints)
    operations = {op.kind.value for candidate in candidates for op in candidate.operations}
    assert "REDUCE_INTENSITY" in operations
    assert "REDUCE_DURATION" in operations


def test_canonical_cascade_disruption_cost_is_19():
    result = run_planner(canonical_state())
    selected = next(c for c in result.candidates if c.candidate_id == result.decision.selected_candidate_id)
    score = next(s for s in result.scores if s.candidate_id == selected.candidate_id)
    assert score.disruption_cost == 19
    moved = {s.logical_session_id: s for s in selected.sessions}
    assert moved["lower-02"].local_date == date(2026, 9, 28)
    assert moved["upper-01"].local_date == date(2026, 10, 1)


def test_disruption_cost_counts_move_removal_addition_and_modification():
    parent = canonical_state().current_plan
    moved = parent[0].model_copy(update={"schedule_window": window(1, 18, 19)})
    removed = parent[1]
    changed = parent[2].model_copy(update={"intensity": Intensity.EASY, "duration_minutes": 45})
    child = (moved, changed, *parent[3:])
    assert _disruption_cost(child, parent) >= 4 + 15 + 5


def test_deterministic_tie_breaking_uses_documented_order_then_hash():
    base = dict(valid=True, goal_alignment=0.8, preference_adherence=1, schedule_fit=1, recovery_fit=0.5, training_continuity=0.8, disruption_cost=4, soft_constraint_penalty=0, total=Decimal("50.000"))
    lower_goal = CandidatePlanScore(**(base | {"candidate_id": "a", "goal_alignment": 0.7}))
    higher_goal = CandidatePlanScore(**base, candidate_id="b")
    assert deterministic_rank_key(higher_goal, "z") < deterministic_rank_key(lower_goal, "a")
    higher_recovery = higher_goal.model_copy(update={"recovery_fit": 0.6})
    lower_disruption = higher_goal.model_copy(update={"disruption_cost": 3})
    assert deterministic_rank_key(higher_recovery, "z") < deterministic_rank_key(higher_goal, "a")
    assert deterministic_rank_key(lower_disruption, "z") < deterministic_rank_key(higher_goal, "a")
    same = higher_goal.model_copy(update={"candidate_id": "c"})
    assert deterministic_rank_key(higher_goal, "a-hash") < deterministic_rank_key(same, "z-hash")


def test_canonical_friday_night_scenario_selects_by_score_not_candidate_label():
    result = run_planner(canonical_state())
    assert result.decision is not None
    assert result.validation.valid
    assert len(result.candidates) >= 3
    selected = next(c for c in result.candidates if c.candidate_id == result.decision.selected_candidate_id)
    selected_score = next(s for s in result.scores if s.candidate_id == selected.candidate_id)
    assert selected_score.total == max(score.total for score in result.scores)
    assert selected_score.total == Decimal("68.055")
    assert selected_score.goal_alignment == 1
    assert selected_score.soft_constraint_penalty == 0
    assert {"lower-02", "upper-01", "run-long-01"}.issubset({s.logical_session_id for s in selected.sessions})
    assert any(op.kind.value == "ADD_RECOVERY" for op in selected.operations)


def test_no_valid_candidate_returns_safe_empty_decision():
    state = canonical_state(available_windows=(), must_preserve_session_ids=tuple(s.logical_session_id for s in canonical_state().current_plan))
    event = LifeEvent(kind="work", starts_at=datetime(2026, 9, 28, 0, tzinfo=timezone.utc), ends_at=datetime(2026, 10, 5, 0, tzinfo=timezone.utc), timezone=ZONE)
    state = state.model_copy(update={"life_events": (event,)})
    result = run_planner(state)
    assert result.decision is None
    assert not result.validation.valid
    assert result.validation.violations == ("NO_VALID_CANDIDATE",)


def test_identical_inputs_produce_identical_outputs():
    state = canonical_state()
    first = run_planner(state).model_dump_json()
    second = run_planner(state).model_dump_json()
    assert first == second
