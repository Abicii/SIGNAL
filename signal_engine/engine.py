from __future__ import annotations

from .candidates import _content_identity, generate_candidates
from .constraints import detect_conflicts, detect_constraints, normalize_state, validate_hard_constraints
from .explanations import derive_reason_codes
from .models import (
    CandidatePlanScore,
    CurrentState,
    PipelineResult,
    PlannerDecision,
    ValidationResult,
)
from .scoring import deterministic_rank_key, score_candidates


def run_planner(
    current_state: CurrentState,
    *,
    max_changed_sessions: int = 3,
    max_candidates: int = 32,
) -> PipelineResult:
    """Run the deterministic MVP stages without persistence or external I/O."""
    normalized = normalize_state(current_state)
    constraints = detect_constraints(normalized)
    conflicts = detect_conflicts(normalized, constraints)
    candidates = generate_candidates(
        normalized,
        constraints,
        max_changed_sessions=max_changed_sessions,
        max_candidates=max_candidates,
    )
    scores = score_candidates(candidates, normalized, constraints)

    if not candidates or not scores:
        validation = ValidationResult(valid=False, violations=("NO_VALID_CANDIDATE",))
        return PipelineResult(
            normalized_state=normalized,
            constraints=constraints,
            conflicts=conflicts,
            candidates=candidates,
            scores=scores,
            decision=None,
            validation=validation,
        )

    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    winner_score: CandidatePlanScore = min(
        scores,
        key=lambda score: deterministic_rank_key(score, candidate_by_id[score.candidate_id].content_hash),
    )
    winner = candidate_by_id[winner_score.candidate_id]
    violations = validate_hard_constraints(
        winner.sessions,
        constraints,
        current_state.horizon_start,
        current_state.horizon_end,
        normalized.plan,
    )
    expected_id, expected_hash = _content_identity(winner.sessions)
    if expected_id != winner.candidate_id or expected_hash != winner.content_hash:
        violations += ("CANDIDATE_CONTENT_HASH_MISMATCH",)
    validation = ValidationResult(
        valid=not violations,
        violations=violations,
        candidate_content_hash=winner.content_hash,
    )
    rationale_codes = derive_reason_codes(current_state, winner, winner_score, scores, conflicts)
    decision = PlannerDecision(
        selected_candidate_id=winner.candidate_id,
        rationale_codes=rationale_codes,
    ) if validation.valid else None
    return PipelineResult(
        normalized_state=normalized,
        constraints=constraints,
        conflicts=conflicts,
        candidates=candidates,
        scores=scores,
        decision=decision,
        validation=validation,
    )
