from evaluation.runner import _service_call, load_scenarios, run_evaluation


def test_golden_scenarios_are_structured_and_cover_required_domains():
    scenarios = load_scenarios()
    assert 30 <= len(scenarios) <= 50
    assert all({"id", "description", "current_plan", "current_state", "goals", "preferences", "expected_invariants"} <= item.keys() for item in scenarios)
    categories = {item["category"] for item in scenarios}
    assert {"recovery", "life_event", "training_conflict", "goals", "preferences", "edge", "lifecycle", "llm"} <= categories
    assert all("life_events" in item["current_state"] for item in scenarios)


def test_golden_scenario_suite_passes_and_reports_metrics():
    metrics = run_evaluation()
    assert not metrics.failures
    assert metrics.scenario_count == 38
    assert metrics.constraint_satisfaction == 100
    assert metrics.plan_validity == 100
    assert 0 <= metrics.goal_preservation <= 100
    assert 0 <= metrics.preference_adherence <= 100
    assert 0 <= metrics.training_continuity <= 100
    assert 0 <= metrics.recovery_fit <= 100
    assert metrics.fallback_validity == 100
    assert metrics.adjudication_attempted == 4
    assert metrics.adjudication_used == 1
    assert metrics.fallback_count == 3
    assert metrics.average_latency_ms > 0
    assert metrics.p95_latency_ms >= metrics.average_latency_ms


def test_every_golden_input_has_identical_repeated_output():
    for scenario in load_scenarios():
        first, _ = _service_call(scenario)
        second, _ = _service_call(scenario)
        assert first.model_dump_json() == second.model_dump_json(), scenario["id"]
