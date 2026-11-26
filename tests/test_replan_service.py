from datetime import datetime, timezone

from signal_engine.engine import run_planner
from signal_engine.models import LifeEvent, ValidationResult
from service.models import ApplicationState, ReplanRequest, ReplanStatus
from service.replan import ReplanService, replan

from tests.test_decision_engine import ZONE, canonical_state


def request_from_state(state):
    return ReplanRequest(
        current_plan=state.current_plan,
        current_state=ApplicationState(
            as_of=state.as_of,
            timezone=state.timezone,
            horizon_start=state.horizon_start,
            horizon_end=state.horizon_end,
            recent_history=state.recent_history,
            available_windows=state.available_windows,
            must_preserve_session_ids=state.must_preserve_session_ids,
        ),
        goals=state.goals,
        preferences=state.preferences,
        life_event=state.life_events[0] if state.life_events else None,
    )


def test_successful_replan_returns_validated_proposal():
    result = replan(request_from_state(canonical_state()))
    assert result.status == ReplanStatus.PROPOSED
    assert result.validation.valid
    assert result.selected_candidate is not None
    assert result.proposed_plan


def test_invalid_application_input_returns_typed_error():
    result = replan({"current_plan": (), "current_state": {"timezone": "not/a-zone"}})
    assert result.status == ReplanStatus.INVALID_INPUT
    assert not result.validation.valid
    assert result.validation_errors


def test_invalid_schedule_window_returns_specific_typed_error():
    payload = request_from_state(canonical_state()).model_dump(mode="python")
    payload["current_plan"] = ({
        "logical_session_id": "bad-window",
        "activity_type": "run",
        "schedule_window": {
            "start_at": datetime(2026, 9, 28, 12),
            "end_at": datetime(2026, 9, 28, 13),
            "timezone": ZONE,
        },
        "intensity": "moderate",
        "duration_minutes": 60,
    },)
    result = replan(payload)
    assert result.status == ReplanStatus.INVALID_SCHEDULE_WINDOW


def test_no_current_plan_is_reported_without_calling_engine():
    result = replan({
        "current_plan": None,
        "current_state": {
            "as_of": datetime(2026, 9, 28, 12, tzinfo=timezone.utc),
            "timezone": ZONE,
            "horizon_start": "2026-09-28",
            "horizon_end": "2026-10-04",
        },
    })
    assert result.status == ReplanStatus.NO_CURRENT_PLAN
    assert result.selected_candidate is None


def test_no_valid_candidate_is_reported_safely():
    source = canonical_state()
    full_horizon_event = LifeEvent(
        kind="work",
        starts_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
        timezone=ZONE,
    )
    blocked = source.model_copy(update={
        "life_events": (full_horizon_event,),
        "must_preserve_session_ids": tuple(session.logical_session_id for session in source.current_plan),
    })
    result = replan(request_from_state(blocked))
    assert result.status == ReplanStatus.NO_VALID_CANDIDATE
    assert result.selected_candidate is None
    assert not result.validation.valid


def test_engine_validation_failure_is_not_returned_as_a_proposal():
    def failing_planner(state):
        pipeline = run_planner(state)
        return pipeline.model_copy(update={
            "decision": None,
            "validation": ValidationResult(valid=False, violations=("TEST_VALIDATION_FAILURE",)),
        })

    result = ReplanService(planner=failing_planner).replan(request_from_state(canonical_state()))
    assert result.status == ReplanStatus.ENGINE_VALIDATION_FAILED
    assert result.proposed_plan is None
    assert result.validation.violations == ("TEST_VALIDATION_FAILURE",)


def test_canonical_scenario_selects_scoring_winner_and_assembles_changes():
    state = canonical_state()
    result = replan(request_from_state(state))
    assert result.status == ReplanStatus.PROPOSED
    assert len(result.candidate_scores) > 1
    assert result.selected_candidate is not None
    selected_score = next(score for score in result.candidate_scores if score.candidate_id == result.selected_candidate.candidate_id)
    assert selected_score.total == max(score.total for score in result.candidate_scores)
    assert result.validation.valid
    assert {change.logical_session_id for change in result.changes} == {"upper-01", "lower-02", "recovery-2026-10-03"}
    assert result.decision_id is None


def test_repeated_execution_is_deterministic():
    request = request_from_state(canonical_state())
    first = replan(request).model_dump_json()
    second = replan(request).model_dump_json()
    assert first == second


def test_result_exposes_future_api_and_persistence_inputs():
    result = replan(request_from_state(canonical_state()))
    assert result.decision_id is None
    assert result.status
    assert result.original_plan
    assert result.proposed_plan
    assert result.changes
    assert result.conflicts
    assert result.candidate_scores
    assert result.reason_codes == result.explanation.reason_codes
    assert result.validation.valid
    assert result.explanation.selected_score is not None
    assert result.input_snapshot is not None
    assert result.input_snapshot.life_event is not None
    assert result.engine_version and result.policy_version
