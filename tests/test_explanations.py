from signal_engine.engine import run_planner
from tests.test_decision_engine import canonical_state


REGISTERED_REASONS = {
    "PRESERVE_PRIMARY_GOAL",
    "RESPECT_CURRENT_INSTRUCTION",
    "REDUCE_RECOVERY_CONFLICT",
    "MINIMIZE_SCHEDULE_DISRUPTION",
    "RESPECT_USER_PREFERENCE",
    "PRESERVE_TRAINING_VOLUME",
    "REDUCE_OVERLOAD",
    "HANDLE_TIME_CONFLICT",
}


def test_canonical_reasons_are_derived_from_selected_score_and_conflict_trace():
    result = run_planner(canonical_state())
    assert result.validation.valid
    assert result.decision is not None
    assert result.decision.rationale_codes == (
        "PRESERVE_PRIMARY_GOAL",
        "REDUCE_RECOVERY_CONFLICT",
        "PRESERVE_TRAINING_VOLUME",
    )


def test_engine_does_not_emit_unregistered_or_unsupported_reason_codes():
    result = run_planner(canonical_state())
    assert result.decision is not None
    assert set(result.decision.rationale_codes) <= REGISTERED_REASONS
    assert "PRESERVE_TRAINING_CONTINUITY" not in result.decision.rationale_codes


def test_identical_engine_inputs_produce_identical_decisions_and_scores():
    state = canonical_state()
    first = run_planner(state)
    second = run_planner(state)
    assert first == second
