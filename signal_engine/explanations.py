"""Deterministic rationale codes derived from the planner's decision trace."""

from __future__ import annotations

from .models import CandidatePlan, CandidatePlanScore, Conflict, CurrentState


def derive_reason_codes(
    state: CurrentState,
    winner: CandidatePlan,
    winner_score: CandidatePlanScore,
    scores: tuple[CandidatePlanScore, ...],
    conflicts: tuple[Conflict, ...],
) -> tuple[str, ...]:
    """Return only registered reasons directly supported by scores/conflicts."""
    reasons: list[str] = []
    if state.goals and winner_score.goal_alignment == max(score.goal_alignment for score in scores):
        if winner_score.goal_alignment == 1:
            reasons.append("PRESERVE_PRIMARY_GOAL")
    if any(conflict.code == "RECOVERY_CONFLICT" for conflict in conflicts):
        if winner_score.soft_constraint_penalty < max(score.soft_constraint_penalty for score in scores):
            reasons.append("REDUCE_RECOVERY_CONFLICT")
    if any(conflict.code == "EVENT_UNAVAILABLE" for conflict in conflicts):
        reasons.append("HANDLE_TIME_CONFLICT")
    if winner_score.training_continuity == 1:
        reasons.append("PRESERVE_TRAINING_VOLUME")

    # The disruption rationale is supported only when disruption is the
    # deciding comparison among candidates equal on every benefit and penalty.
    equivalent = [score for score in scores if (
        score.goal_alignment == winner_score.goal_alignment
        and score.preference_adherence == winner_score.preference_adherence
        and score.schedule_fit == winner_score.schedule_fit
        and score.recovery_fit == winner_score.recovery_fit
        and score.training_continuity == winner_score.training_continuity
        and score.soft_constraint_penalty == winner_score.soft_constraint_penalty
    )]
    if len(equivalent) > 1 and winner_score.disruption_cost == min(score.disruption_cost for score in equivalent):
        reasons.append("MINIMIZE_SCHEDULE_DISRUPTION")
    return tuple(reasons)
