from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class EvaluationMetrics:
    scenario_count: int
    constraint_satisfaction: float
    plan_validity: float
    goal_preservation: float
    preference_adherence: float
    training_continuity: float
    recovery_fit: float
    fallback_validity: float
    adjudication_attempted: int
    adjudication_used: int
    fallback_count: int
    average_latency_ms: float
    p95_latency_ms: float
    failures: tuple[str, ...]


def percentile95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, int((len(ordered) * 0.95 + 0.999999)) - 1)]


def average(values: list[float]) -> float:
    return mean(values) if values else 0.0
