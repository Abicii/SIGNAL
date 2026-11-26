import asyncio
import os
from datetime import datetime

import httpx
import pytest

from app import main
from persistence import SignalRepository
from service.replan import ReplanService
from tests.persistence_helpers import prepare_repository


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    database_url = os.environ.get("SIGNAL_TEST_DATABASE_URL", "sqlite://")
    repository = SignalRepository(database_url)
    prepare_repository(repository, reset=database_url != "sqlite://")
    monkeypatch.setattr(main, "repository", repository)
    yield
    repository.dispose()


async def _send(method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def request(method: str, path: str, **kwargs):
    return asyncio.run(_send(method, path, **kwargs))


def reset_sample_state():
    # Each test receives a freshly seeded database through isolated_database.
    pass


def test_today_and_plan_endpoints_return_sample_week(monkeypatch):
    class FixtureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 9, 30, 12, 0)
            return value.replace(tzinfo=tz) if tz is not None else value

    monkeypatch.setattr(main, "datetime", FixtureDateTime)
    reset_sample_state()
    today = request("GET", "/api/v1/today")
    plan = request("GET", "/api/v1/plan")
    assert today.status_code == 200
    assert today.json()["session"]["label"] == "Lower"
    assert today.json()["weekly_progress"] == {"completed": 2, "total": 6}
    assert plan.status_code == 200
    assert len(plan.json()["days"]) == 7
    assert plan.json()["days"][3]["label"] == "Rest"


def test_replan_feedback_flow_uses_service_result():
    reset_sample_state()
    response = request("POST", "/api/v1/replan", json={
        "kind": "social",
        "starts_at": "2026-10-02T23:00:00",
        "ends_at": "2026-10-03T06:00:00",
        "impact": "high",
    })
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["status"] == "proposed"
    assert result["validation"]["valid"]
    assert len(result["candidate_scores"]) > 1
    assert response.json()["signal_moment"]["type"] == "recovery_after_late_night"
    assert "recovery" in response.json()["explanation"].lower()
    assert "Lower moved from Saturday to Monday" in response.json()["explanation"]
    assert "REDUCE_RECOVERY_CONFLICT" in result["reason_codes"]
    assert "PRESERVE_PRIMARY_GOAL" in result["reason_codes"]
    assert "PRESERVE_TRAINING_CONTINUITY" not in result["reason_codes"]

    accepted = request("POST", "/api/v1/feedback", json={"action": "accept", "decision_id": result["decision_id"]})
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    updated_plan = request("GET", "/api/v1/plan").json()
    assert next(day for day in updated_plan["days"] if day["date"] == "2026-10-03")["label"] == "Recovery"


def test_rejected_proposal_keeps_original_plan():
    reset_sample_state()
    original = request("GET", "/api/v1/plan").json()
    proposal = request("POST", "/api/v1/replan", json={
        "kind": "social",
        "starts_at": "2026-10-02T23:00:00",
        "ends_at": "2026-10-03T06:00:00",
        "impact": "high",
    })
    assert proposal.status_code == 200
    rejected = request("POST", "/api/v1/feedback", json={"action": "reject", "decision_id": proposal.json()["result"]["decision_id"]})
    assert rejected.status_code == 200
    assert request("GET", "/api/v1/plan").json()["days"] == original["days"]


@pytest.mark.parametrize("kind,starts_at,ends_at", [
    ("poor_sleep", "2026-09-30T00:00:00", "2026-09-30T06:00:00"),
    ("soreness", "2026-09-30T07:00:00", "2026-09-30T08:00:00"),
    ("missed_workout", "2026-10-02T18:00:00", "2026-10-02T18:45:00"),
])
def test_replan_accepts_each_supported_trigger_kind(kind, starts_at, ends_at):
    reset_sample_state()
    response = request("POST", "/api/v1/replan", json={
        "kind": kind,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "impact": "high",
    })
    assert response.status_code == 200, response.text
    assert response.json()["result"]["validation"]["valid"]


@pytest.mark.parametrize("kind,expected_metric,expected_value", [
    ("poor_sleep", "sleep_duration", 6.0),
    ("soreness", "soreness", 8.0),
])
def test_self_reported_inputs_are_converted_to_engine_observations(monkeypatch, kind, expected_metric, expected_value):
    reset_sample_state()
    captured = {}
    original = ReplanService.replan

    def capture(service, request):
        captured["request"] = request
        return original(service, request)

    monkeypatch.setattr(ReplanService, "replan", capture)
    response = request("POST", "/api/v1/replan", json={
        "kind": kind,
        "starts_at": "2026-09-30T00:00:00",
        "ends_at": "2026-09-30T06:00:00",
        "impact": "high",
    })
    assert response.status_code == 200, response.text
    observations = captured["request"].current_state.recent_history.physiology
    observation = next(item for item in observations if item.metric == expected_metric)
    assert observation.value == expected_value
    assert observation.quality == "self_reported"


def test_social_event_impact_does_not_change_engine_decision(monkeypatch):
    reset_sample_state()
    body = {
        "kind": "social",
        "starts_at": "2026-10-02T23:00:00",
        "ends_at": "2026-10-03T06:00:00",
    }
    low = request("POST", "/api/v1/replan", json={**body, "impact": "low"}).json()["result"]
    reset_sample_state()
    high = request("POST", "/api/v1/replan", json={**body, "impact": "high"}).json()["result"]
    assert low["selected_candidate"]["candidate_id"] == high["selected_candidate"]["candidate_id"]
    assert low["candidate_scores"] == high["candidate_scores"]


def test_non_recovery_replan_signal_moment_is_based_on_actual_plan_change():
    reset_sample_state()
    response = request("POST", "/api/v1/replan", json={
        "kind": "work",
        "starts_at": "2026-10-02T18:00:00",
        "ends_at": "2026-10-02T19:00:00",
    })
    assert response.status_code == 200
    assert response.json()["signal_moment"]["type"] != "recovery_after_late_night"
