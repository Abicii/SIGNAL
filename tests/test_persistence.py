from datetime import datetime, timezone
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy import event as sqlalchemy_event

from persistence import PersistenceError, ProposalNotFound, SignalRepository, StaleProposal
from persistence.models import DecisionRunRecord, PlanChangeRecord, PlanVersionRecord, PlannedSessionRecord
from service.replan import replan
from tests.test_replan_service import request_from_state
from tests.test_decision_engine import canonical_state
from tests.persistence_helpers import prepare_repository


@pytest.fixture
def repository():
    database_url = os.environ.get("SIGNAL_TEST_DATABASE_URL", "sqlite://")
    store = SignalRepository(database_url)
    prepare_repository(store, reset=database_url != "sqlite://")
    yield store
    store.dispose()


def create_canonical_proposal(store):
    state = canonical_state()
    _, parent_id, _ = store.current_plan()
    result = replan(request_from_state(state))
    persisted = store.create_proposal(result, expected_parent_id=parent_id, trigger_kind="life_event")
    return persisted, parent_id


def test_create_proposed_plan_and_decision_run(repository):
    result, parent_id = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)
    plan = repository.get_plan_version(run.result_plan_version_id)
    assert plan["status"] == "proposed"
    assert repository.current_plan()[1] == parent_id
    assert run.status == "proposed"
    assert run.parent_plan_version_id == parent_id
    assert run.input_snapshot["life_event"]["kind"] == "social"
    assert len(run.candidate_scores) == len(result.candidate_scores) > 1
    assert any(row["candidate"] for row in run.candidate_scores)


def test_accept_updates_pointer_and_supersedes_previous_version(repository):
    result, parent_id = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)
    status, current = repository.decide_proposal(result.decision_id, "accept")
    accepted = repository.get_plan_version(run.result_plan_version_id)
    previous = repository.get_plan_version(parent_id)
    assert status == "accepted"
    assert accepted["status"] == "accepted"
    assert previous["status"] == "superseded"
    assert {item.logical_session_id for item in current} == {item.logical_session_id for item in result.proposed_plan}
    assert repository.current_plan()[1] == run.result_plan_version_id


def test_reject_preserves_current_plan_pointer(repository):
    result, parent_id = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)
    status, current = repository.decide_proposal(result.decision_id, "reject")
    rejected = repository.get_plan_version(run.result_plan_version_id)
    assert status == "rejected"
    assert rejected["status"] == "rejected"
    assert repository.current_plan()[1] == parent_id
    assert {item.logical_session_id for item in current} == {item.logical_session_id for item in result.original_plan}
    assert repository.get_decision_run(result.decision_id).user_outcome == "rejected"


def test_plan_changes_persist_with_versioned_session_references(repository):
    result, _ = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)
    changes = repository.plan_changes(run.result_plan_version_id)
    assert len(changes) == len(result.changes)
    assert all(change.before_snapshot or change.after_snapshot for change in changes)
    assert all(change.reason_code in result.reason_codes for change in changes)
    lower_change = next(change for change in changes if change.logical_session_id == "lower-02")
    assert lower_change.prior_session_id is not None
    assert lower_change.session_id is not None


def test_logical_session_identity_is_preserved_across_plan_versions(repository):
    result, _ = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)
    child = repository.get_plan_version(run.result_plan_version_id)
    assert {item.logical_session_id for item in child["sessions"]} == {item.logical_session_id for item in result.original_plan} | {"recovery-2026-10-03"}


def test_stale_acceptance_is_rejected_and_does_not_overwrite_newer_plan(repository):
    first, parent_id = create_canonical_proposal(repository)
    second, _ = create_canonical_proposal(repository)
    second_run = repository.get_decision_run(second.decision_id)
    repository.decide_proposal(first.decision_id, "accept")
    current_id = repository.current_plan()[1]
    with pytest.raises(StaleProposal):
        repository.decide_proposal(second.decision_id, "accept")
    assert repository.current_plan()[1] == current_id
    assert repository.get_plan_version(second_run.result_plan_version_id)["status"] == "superseded"
    assert repository.get_decision_run(second.decision_id).user_outcome == "expired"
    assert repository.get_plan_version(parent_id)["status"] == "superseded"


def test_accepted_plan_survives_repository_restart(tmp_path):
    path = tmp_path / "signal.sqlite"
    store = SignalRepository(f"sqlite:///{path.as_posix()}")
    prepare_repository(store)
    result, _ = create_canonical_proposal(store)
    run = store.get_decision_run(result.decision_id)
    store.decide_proposal(result.decision_id, "accept")
    accepted_id = run.result_plan_version_id
    store.dispose()

    restarted = SignalRepository(f"sqlite:///{path.as_posix()}")
    restarted.initialize()
    current_plan, current_id, _ = restarted.current_plan()
    assert current_id == accepted_id
    assert current_plan
    restarted.dispose()


def test_proposal_creation_rejects_parent_that_is_no_longer_current(repository):
    result, parent_id = create_canonical_proposal(repository)
    repository.decide_proposal(result.decision_id, "accept")
    with pytest.raises(StaleProposal):
        repository.create_proposal(result, expected_parent_id=parent_id, trigger_kind="life_event")


def test_proposal_write_rolls_back_every_table_on_mid_transaction_failure(repository, monkeypatch):
    result = replan(request_from_state(canonical_state()))
    _, parent_id, _ = repository.current_plan()

    def fail_change_insert(*_args, **_kwargs):
        raise PersistenceError("injected PlanChange persistence failure")

    monkeypatch.setattr(repository, "_insert_changes", fail_change_insert)
    with pytest.raises(PersistenceError):
        repository.create_proposal(result, expected_parent_id=parent_id, trigger_kind="life_event")
    assert repository.current_plan()[1] == parent_id
    with repository._sessions() as db:
        assert db.scalar(select(func.count()).select_from(PlanVersionRecord)) == 1
        assert db.scalar(select(func.count()).select_from(DecisionRunRecord)) == 0
        assert db.scalar(select(func.count()).select_from(PlannedSessionRecord)) == len(result.original_plan)
        assert db.scalar(select(func.count()).select_from(PlanChangeRecord)) == 0


def test_acceptance_rolls_back_pointer_and_statuses_on_transaction_failure(repository):
    result, parent_id = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)

    def fail_accept(_mapper, _connection, target):
        if target.status == "accepted":
            raise RuntimeError("injected accept persistence failure")

    sqlalchemy_event.listen(PlanVersionRecord, "before_update", fail_accept)
    try:
        with pytest.raises(RuntimeError, match="injected accept persistence failure"):
            repository.decide_proposal(result.decision_id, "accept")
    finally:
        sqlalchemy_event.remove(PlanVersionRecord, "before_update", fail_accept)

    assert repository.current_plan()[1] == parent_id
    assert repository.get_plan_version(run.result_plan_version_id)["status"] == "proposed"
    assert repository.get_plan_version(parent_id)["status"] == "accepted"
    assert repository.get_decision_run(result.decision_id).user_outcome == "pending"


def test_missing_or_already_decided_proposal_returns_typed_error(repository):
    with pytest.raises(ProposalNotFound):
        repository.decide_proposal("00000000-0000-0000-0000-000000000000", "accept")
    result, _ = create_canonical_proposal(repository)
    repository.decide_proposal(result.decision_id, "reject")
    with pytest.raises(ProposalNotFound):
        repository.decide_proposal(result.decision_id, "accept")


def test_schedule_instants_are_saved_in_utc_and_timezone_is_separate(repository):
    result, _ = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)
    persisted = repository.get_plan_version(run.result_plan_version_id)
    lower = next(item for item in persisted["sessions"] if item.logical_session_id == "lower-02")
    assert lower.schedule_window.timezone == "Asia/Calcutta"
    assert lower.schedule_window.start_at.utcoffset() == datetime.now(timezone.utc).utcoffset()


def test_canonical_friday_scenario_has_plan_changes_and_full_candidate_trace(repository):
    result, _ = create_canonical_proposal(repository)
    run = repository.get_decision_run(result.decision_id)
    assert run.validation["valid"] is True
    assert run.adjudication["selected_candidate_id"] == result.selected_candidate.candidate_id
    assert any(
        row["candidate"]["candidate_id"] == result.selected_candidate.candidate_id
        and row["score"]["candidate_id"] == result.selected_candidate.candidate_id
        for row in run.candidate_scores
    )
    assert len(repository.plan_changes(run.result_plan_version_id)) > 0
