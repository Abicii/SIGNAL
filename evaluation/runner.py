from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from service.llm import LLMDecision, MockLLMProvider
from service.models import ReplanRequest, ReplanStatus
from service.replan import ReplanService
from signal_engine.models import CurrentState, PhysiologyObservation, RecentHistory, TrainingOutcome
from evaluation.metrics.report import EvaluationMetrics, average, percentile95


FIXTURE = Path(__file__).parent / "scenarios" / "golden.json"


class _FailureProvider:
    provider_name = "evaluation-failure"
    model_name = "fixture"

    async def generate_structured(self, _context):
        raise RuntimeError("synthetic provider failure")


class _ResponseProvider:
    provider_name = "evaluation-response"
    model_name = "fixture"

    def __init__(self, response: Any):
        self.response = response

    async def generate_structured(self, _context):
        return self.response


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = copy.deepcopy(value)
    return result


def load_scenarios() -> list[dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    defaults = data["defaults"]
    expanded: list[dict[str, Any]] = []
    for definition in data["scenarios"]:
        scenario = _merge(defaults, definition)
        scenario["schema_version"] = data["schema_version"]
        scenario["current_state"] = copy.deepcopy(scenario["current_state"])
        scenario["current_state"]["life_events"] = copy.deepcopy(scenario.get("events", scenario.get("life_events", [])))
        scenario["current_state"].pop("events", None)
        scenario["current_state"]["must_preserve_session_ids"] = scenario.get(
            "preserve", scenario["current_state"].get("must_preserve_session_ids", [])
        )
        if scenario.get("no_plan"):
            scenario["current_plan"] = None
        if scenario.get("all_preserved"):
            scenario["current_state"]["must_preserve_session_ids"] = [
                item["logical_session_id"] for item in scenario["current_plan"]
            ]
        if "hard_days" in scenario:
            scenario["preferences"] = [{"days": scenario["hard_days"], "strength": "hard"}]
        if scenario.get("close_lower_sessions"):
            lower = next(item for item in scenario["current_plan"] if item["logical_session_id"] == "lower-02")
            lower["schedule_window"] = {
                "start_at": "2026-10-01T18:00:00+05:30", "end_at": "2026-10-01T19:00:00+05:30", "timezone": "Asia/Calcutta"
            }
        if scenario.get("hard_consecutive"):
            for item in scenario["current_plan"]:
                if item["logical_session_id"] in {"upper-01", "run-01"}:
                    item["intensity"] = "hard"
        if scenario.get("stale_plan"):
            scenario["current_plan"][0]["schedule_window"]["start_at"] = "2026-09-20T12:30:00Z"
            scenario["current_plan"][0]["schedule_window"]["end_at"] = "2026-09-20T13:30:00Z"
        scenario["current_state"]["recent_history"] = _history(scenario)
        expanded.append(scenario)
    return expanded


def _history(scenario: dict[str, Any]) -> dict[str, Any]:
    now = datetime.fromisoformat("2026-09-28T12:00:00+00:00")
    physiology: list[dict[str, Any]] = []
    if "sleep_hours" in scenario:
        observed = now - (timedelta(days=10) if scenario.get("stale_sleep") else timedelta(hours=8))
        for offset in range(7):
            value = scenario["sleep_hours"] if scenario.get("repeated_sleep") or offset == 0 else 7.5
            physiology.append({"metric": "sleep_duration", "value": value, "unit": "h", "observed_at": (observed - timedelta(days=offset)).isoformat(), "quality": "self_reported"})
    if "soreness" in scenario:
        physiology.append({"metric": "soreness", "value": scenario["soreness"], "unit": "0-10", "observed_at": (now - timedelta(hours=8)).isoformat()})
    training: list[dict[str, Any]] = []
    week_ends: list[str] = []
    if scenario.get("recent_load"):
        for index in range(4):
            end = now - timedelta(days=7 * index)
            week_ends.append(end.isoformat())
        for index in range(6):
            training.append({"occurred_at": (now - timedelta(days=index)).isoformat(), "load_value": 100, "load_unit": "session_rpe_load", "quality": "self_reported"})
        for week in range(1, 5):
            training.append({"occurred_at": (now - timedelta(days=7 * week + 1)).isoformat(), "load_value": 30, "load_unit": "session_rpe_load", "quality": "self_reported"})
    return {"physiology": physiology, "training": training, "complete_training_week_ends": week_ends}


def _request(scenario: dict[str, Any]) -> ReplanRequest:
    current_state = dict(scenario["current_state"])
    events = current_state.pop("life_events", [])
    goals = scenario.get("goals", [])
    preferences = scenario.get("preferences", [])
    return ReplanRequest.model_validate({
        "current_plan": scenario.get("current_plan"),
        "current_state": current_state,
        "goals": goals,
        "preferences": preferences,
        "life_event": events[0] if len(events) == 1 else None,
    }) if len(events) <= 1 else _multi_event_request(scenario, current_state, goals, preferences, events)


def _multi_event_request(scenario, current_state, goals, preferences, events):
    # The application service contract currently accepts one event; multi-event
    # cases exercise the engine contract directly without changing that API.
    state = CurrentState.model_validate({
        **current_state, "current_plan": scenario["current_plan"], "goals": goals,
        "preferences": preferences, "life_events": events,
    })
    return state


def _service_call(scenario: dict[str, Any], provider=None):
    request = _request(scenario)
    if isinstance(request, CurrentState):
        from signal_engine.engine import run_planner
        pipeline = run_planner(request)
        candidate = next((item for item in pipeline.candidates if pipeline.decision and item.candidate_id == pipeline.decision.selected_candidate_id), None)
        selected_score = next((item for item in pipeline.scores if candidate and item.candidate_id == candidate.candidate_id), None)
        result = SimpleNamespace(
            model_dump_json=lambda: pipeline.model_dump_json(), status=ReplanStatus.PROPOSED if pipeline.validation.valid else ReplanStatus.NO_VALID_CANDIDATE,
            validation=pipeline.validation, candidate_scores=pipeline.scores, proposed_plan=candidate.sessions if candidate else None,
            selected_candidate=candidate, explanation=SimpleNamespace(selected_score=selected_score),
            adjudication=SimpleNamespace(adjudication_attempted=False, adjudication_used=False, fallback_used=False),
        )
        return result, request
    return ReplanService(provider=provider).replan(request), request.to_domain() if request.current_plan is not None else None


def _evaluate_invariants(scenario: dict[str, Any], result, state) -> list[str]:
    failures: list[str] = []
    invariants = scenario.get("expected_invariants", [])
    status = getattr(result, "status", None)
    if "no_current_plan" in invariants and status != ReplanStatus.NO_CURRENT_PLAN:
        failures.append("expected no_current_plan status")
    if "no_candidate_safe_failure" in invariants and status != ReplanStatus.NO_VALID_CANDIDATE:
        failures.append(f"expected no_valid_candidate, got {status}")
    if "inconsistent_state" in invariants and status != ReplanStatus.INCONSISTENT_STATE:
        failures.append(f"expected inconsistent_state, got {status}")
    if "valid_if_proposed" in invariants and status == ReplanStatus.PROPOSED and not result.validation.valid:
        failures.append("proposal did not pass validation")
    if "preserve_long_run" in invariants and status == ReplanStatus.PROPOSED:
        selected = {item.logical_session_id for item in result.proposed_plan}
        if "run-long-01" not in selected:
            failures.append("Sunday long run was not preserved")
    if "no_event_overlap" in invariants and status == ReplanStatus.PROPOSED and state is not None:
        from signal_engine.constraints import normalize_state
        from signal_engine.constraints import _window_overlap
        normalized = normalize_state(state)
        for session in result.proposed_plan:
            for event in normalized.events:
                from signal_engine.models import ScheduleWindow
                window = ScheduleWindow(start_at=event.starts_at, end_at=event.ends_at, timezone=event.timezone)
                if _window_overlap(session.schedule_window, window):
                    failures.append(f"session {session.logical_session_id} overlaps an event")
                    break
    if "hard_constraints_only" in invariants and status == ReplanStatus.PROPOSED and not result.validation.valid:
        failures.append("hard preference proposal failed validation")
    if "identical_candidate_scores" in invariants:
        totals = [score.total for score in getattr(result, "candidate_scores", ())]
        if len(set(totals)) == len(totals):
            failures.append("fixture did not produce equal candidate scores")
    return failures


def _provider_for(scenario: dict[str, Any], deterministic_result):
    mode = scenario.get("adjudication")
    if mode is None:
        return None
    if mode == "mock":
        return MockLLMProvider()
    if mode == "failure":
        return _FailureProvider()
    if mode == "malformed":
        return _ResponseProvider({"selected_candidate_id": "bad", "reason_codes": [], "unexpected": True})
    if mode == "invalid":
        return _ResponseProvider(LLMDecision(selected_candidate_id="unknown-candidate", reason_codes=()))
    raise ValueError(f"unknown evaluation adjudication mode {mode}")


def run_evaluation() -> EvaluationMetrics:
    scenarios = load_scenarios()
    failures: list[str] = []
    latencies: list[float] = []
    scores: list[Any] = []
    valid_count = hard_valid_count = proposed_count = 0
    fallback_valid = fallback_count = 0
    attempted = used = 0
    signatures: dict[str, str] = {}

    for scenario in scenarios:
        start = time.perf_counter()
        result, state = _service_call(scenario)
        elapsed = (time.perf_counter() - start) * 1000
        latencies.append(elapsed)
        errors = _evaluate_invariants(scenario, result, state)
        failures.extend(f"{scenario['id']}: {error}" for error in errors)

        if isinstance(result, object) and hasattr(result, "model_dump_json"):
            signature = result.model_dump_json()
            if scenario["id"] in signatures and signatures[scenario["id"]] != signature:
                failures.append(f"{scenario['id']}: repeated output was not deterministic")
            signatures[scenario["id"]] = signature
            second = _service_call(scenario)[0]
            if second.model_dump_json() != signature:
                failures.append(f"{scenario['id']}: identical input produced different output")
            trace = result.adjudication
            attempted += int(trace.adjudication_attempted)
            used += int(trace.adjudication_used)
            fallback_count += int(trace.fallback_used)
            if result.status == ReplanStatus.PROPOSED:
                proposed_count += 1
                valid = bool(result.validation.valid)
                valid_count += int(valid)
                hard_valid_count += int(valid)
                if result.explanation.selected_score is not None:
                    scores.append(result.explanation.selected_score)

            provider = _provider_for(scenario, result)
            if provider is not None:
                adjudicated = ReplanService(provider=provider).replan(_request(scenario))
                attempted += int(adjudicated.adjudication.adjudication_attempted)
                used += int(adjudicated.adjudication.adjudication_used)
                fallback_count += int(adjudicated.adjudication.fallback_used)
                if adjudicated.adjudication.fallback_used:
                    is_valid_fallback = adjudicated.status == ReplanStatus.PROPOSED and adjudicated.validation.valid
                    fallback_valid += int(is_valid_fallback)
                    if not is_valid_fallback:
                        failures.append(f"{scenario['id']}: fallback failed validation")
                elif scenario["category"] == "llm" and adjudicated.status != ReplanStatus.PROPOSED:
                    failures.append(f"{scenario['id']}: adjudicated proposal failed")
                if adjudicated.status == ReplanStatus.PROPOSED and (
                    adjudicated.selected_candidate is None or not adjudicated.selected_candidate.valid
                ):
                    failures.append(f"{scenario['id']}: adjudicator selected invalid candidate")

    return EvaluationMetrics(
        scenario_count=len(scenarios),
        constraint_satisfaction=100 * hard_valid_count / proposed_count if proposed_count else 100.0,
        plan_validity=100 * valid_count / proposed_count if proposed_count else 100.0,
        goal_preservation=100 * average([score.goal_alignment for score in scores]),
        preference_adherence=100 * average([score.preference_adherence for score in scores]),
        training_continuity=100 * average([score.training_continuity for score in scores]),
        recovery_fit=100 * average([score.recovery_fit for score in scores]),
        fallback_validity=100 * fallback_valid / fallback_count if fallback_count else 100.0,
        adjudication_attempted=attempted,
        adjudication_used=used,
        fallback_count=fallback_count,
        average_latency_ms=average(latencies),
        p95_latency_ms=percentile95(latencies),
        failures=tuple(failures),
    )


def main() -> int:
    metrics = run_evaluation()
    print("SIGNAL Evaluation")
    print("-----------------")
    print(f"Scenarios: {metrics.scenario_count}")
    print(f"Constraint satisfaction: {metrics.constraint_satisfaction:.1f}%")
    print(f"Plan validity: {metrics.plan_validity:.1f}%")
    print(f"Goal preservation: {metrics.goal_preservation:.1f}%")
    print(f"Preference adherence: {metrics.preference_adherence:.1f}%")
    print(f"Training continuity: {metrics.training_continuity:.1f}%")
    print(f"Recovery fit: {metrics.recovery_fit:.1f}%")
    print(f"Fallback validity: {metrics.fallback_validity:.1f}%")
    print(f"Deterministic latency: avg {metrics.average_latency_ms:.2f} ms, p95 {metrics.p95_latency_ms:.2f} ms")
    print("LLM adjudication:")
    print(f"Attempted: {metrics.adjudication_attempted}")
    print(f"Used: {metrics.adjudication_used}")
    print(f"Fallback: {metrics.fallback_count}")
    print("Failures:")
    if metrics.failures:
        for failure in metrics.failures:
            print(f"- {failure}")
        return 1
    print("- none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
