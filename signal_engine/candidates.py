from __future__ import annotations

from collections import deque
from datetime import timedelta, timezone
from hashlib import sha256
import json
from types import MappingProxyType

from .constraints import validate_hard_constraints
from .models import (
    CandidateOperation,
    CandidatePlan,
    Constraint,
    CurrentState,
    Intensity,
    NormalizedState,
    OperationKind,
    PlannedSession,
    ScheduleWindow,
)


MAX_CHANGED_SESSIONS = 3
MAX_CANDIDATES = 32
_INTENSITY_REDUCTIONS = MappingProxyType({
    Intensity.HARD: (Intensity.MODERATE, Intensity.EASY),
    Intensity.MODERATE: (Intensity.EASY,),
})


def _canonical_sessions(sessions: tuple[PlannedSession, ...]) -> tuple[PlannedSession, ...]:
    return tuple(sorted(sessions, key=lambda s: (s.local_date, s.schedule_window.start_at, s.logical_session_id)))


def _content_identity(sessions: tuple[PlannedSession, ...]) -> tuple[str, str]:
    payload = []
    for session in _canonical_sessions(sessions):
        row = session.model_dump(mode="json")
        row["schedule_window"]["start_at"] = session.schedule_window.start_at.astimezone(timezone.utc).isoformat()
        row["schedule_window"]["end_at"] = session.schedule_window.end_at.astimezone(timezone.utc).isoformat()
        payload.append(row)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    return f"cand-{digest[:12]}", digest


def _changed_ids(before: tuple[PlannedSession, ...], after: tuple[PlannedSession, ...]) -> set[str]:
    before_map = {s.logical_session_id: s for s in before}
    after_map = {s.logical_session_id: s for s in after}
    return {key for key in before_map.keys() | after_map.keys() if before_map.get(key) != after_map.get(key)}


def _duration_fits(session: PlannedSession, window: ScheduleWindow) -> bool:
    seconds = (window.end_at - window.start_at).total_seconds()
    return seconds == session.duration_minutes * 60


def _replace_session(
    sessions: tuple[PlannedSession, ...],
    logical_id: str,
    replacement: PlannedSession | None,
) -> tuple[PlannedSession, ...]:
    values = [s for s in sessions if s.logical_session_id != logical_id]
    if replacement is not None:
        values.append(replacement)
    return _canonical_sessions(tuple(values))


def _candidate(
    sessions: tuple[PlannedSession, ...],
    operations: tuple[CandidateOperation, ...],
    source: CurrentState,
) -> CandidatePlan:
    candidate_id, digest = _content_identity(sessions)
    changed = _changed_ids(source.current_plan, sessions)
    preserved = tuple(
        CandidateOperation(kind=OperationKind.PRESERVE_SESSION, logical_session_ids=(s.logical_session_id,))
        for s in source.current_plan if s.logical_session_id not in changed
    )
    effective_ops = operations or (CandidateOperation(kind=OperationKind.NO_CHANGE),)
    return CandidatePlan(
        candidate_id=candidate_id,
        content_hash=digest,
        sessions=_canonical_sessions(sessions),
        operations=effective_ops + preserved,
    )


def _legal(
    sessions: tuple[PlannedSession, ...],
    normalized: NormalizedState,
    constraints: tuple[Constraint, ...],
) -> bool:
    return not validate_hard_constraints(
        sessions,
        constraints,
        normalized.source.horizon_start,
        normalized.source.horizon_end,
        normalized.plan,
    )


def _requires_recovery_entry(sessions: tuple[PlannedSession, ...], normalized: NormalizedState) -> bool:
    """Materialize recovery as a session when the late-event buffer is otherwise clear.

    This small-slice assumption follows the canonical example's explicit recovery
    entry. The unchanged/high-load-in-buffer plan remains a candidate and is scored
    with the documented penalty.
    """
    if not normalized.recovery_windows:
        return False
    has_high_load = any(
        s.intensity in {Intensity.MODERATE, Intensity.HARD}
        and any(_overlaps(s.schedule_window, window) for window in normalized.recovery_windows)
        for s in sessions
    )
    has_recovery_entry = any(
        s.intensity in {Intensity.RECOVERY, Intensity.REST}
        and any(_overlaps(s.schedule_window, window) for window in normalized.recovery_windows)
        for s in sessions
    )
    return not has_high_load and not has_recovery_entry


def _overlaps(left: ScheduleWindow, right: ScheduleWindow) -> bool:
    return left.start_at < right.end_at and right.start_at < left.end_at


def generate_candidates(
    normalized: NormalizedState,
    constraints: tuple[Constraint, ...],
    max_changed_sessions: int = MAX_CHANGED_SESSIONS,
    max_candidates: int = MAX_CANDIDATES,
) -> tuple[CandidatePlan, ...]:
    """Breadth-first deterministic search; only feasible schedules are returned.

    Only explicit `available_windows` are move targets. The search state itself is
    never exposed; every returned candidate has passed hard-constraint validation.
    Intensity reduction is offered for run sessions as a conservative MVP catalog
    rule; schedule-only recovery buffers are handled by moves/recovery insertion.
    """
    source = normalized.source
    base = normalized.plan
    # Focus the bounded search on sessions implicated by detected time/day rules
    # plus sessions occupying their explicitly supplied destination windows.
    # This mirrors the spec's conflict-directed edit operators and prevents a
    # 32-candidate cap from being consumed by unrelated workout edits.
    focus_ids: set[str] = set()
    for constraint in constraints:
        if constraint.window is not None:
            focus_ids.update(
                s.logical_session_id for s in base
                if s.schedule_window.start_at < constraint.window.end_at
                and constraint.window.start_at < s.schedule_window.end_at
            )
        if constraint.code == "PREFERRED_TRAINING_DAYS":
            focus_ids.update(
                s.logical_session_id for s in base
                if s.intensity not in {Intensity.REST, Intensity.RECOVERY}
                and s.local_date.isoweekday() not in set(constraint.parameters["days"])
            )
    if not focus_ids:
        focus_ids = {s.logical_session_id for s in base}
    blocker_ids = {
        s.logical_session_id for s in base if s.logical_session_id not in focus_ids
        and any(
            s.schedule_window.start_at < target.end_at and target.start_at < s.schedule_window.end_at
            for focus_id in focus_ids
            for focused in base if focused.logical_session_id == focus_id
            for target in source.available_windows
            if _duration_fits(focused, target)
        )
    }
    editable_ids = focus_ids | blocker_ids
    if not _legal(base, normalized, constraints):
        # A conflicting starting plan is not a valid candidate. We still search
        # from it, but never return it or any intermediate invalid schedule.
        initial_legal = False
    else:
        initial_legal = True

    queue = deque([(base, tuple())])
    visited = {_content_identity(base)[1]}
    found: dict[str, CandidatePlan] = {}

    if initial_legal:
        item = _candidate(base, tuple(), source)
        found[item.content_hash] = item

    while queue and len(found) < max_candidates:
        sessions, operations = queue.popleft()
        already_operated = {logical_id for op in operations for logical_id in op.logical_session_ids}
        changed = _changed_ids(source.current_plan, sessions)
        if len(changed) >= max_changed_sessions:
            continue

        transitions: list[tuple[tuple[PlannedSession, ...], CandidateOperation]] = []
        by_id = {s.logical_session_id: s for s in sessions}

        for session in sessions:
            if session.logical_session_id not in editable_ids or session.logical_session_id in already_operated:
                continue
            for window in normalized.source.available_windows:
                if window == session.schedule_window or not _duration_fits(session, window):
                    continue
                moved = session.model_copy(update={"schedule_window": window})
                new_sessions = _replace_session(sessions, session.logical_session_id, moved)
                transitions.append((new_sessions, CandidateOperation(
                    kind=OperationKind.MOVE_SESSION,
                    logical_session_ids=(session.logical_session_id,),
                    detail=f"{session.local_date.isoformat()}->{moved.local_date.isoformat()}",
                )))

        # Removal is available only for non-protected sessions; goal loss is scored.
        for session in sessions:
            if session.logical_session_id not in editable_ids or session.logical_session_id in already_operated:
                continue
            if session.logical_session_id not in source.must_preserve_session_ids:
                transitions.append((
                    _replace_session(sessions, session.logical_session_id, None),
                    CandidateOperation(kind=OperationKind.REMOVE_SESSION, logical_session_ids=(session.logical_session_id,)),
                ))

        # Run intensity changes are supported; a schedule-only recovery buffer
        # does not by itself justify lowering training intensity in this MVP slice.
        for session in sessions:
            if session.logical_session_id not in editable_ids or session.logical_session_id in already_operated:
                continue
            if session.activity_type.lower() not in {"run", "long_run", "running"}:
                continue
            for intensity in _INTENSITY_REDUCTIONS.get(session.intensity, ()):
                changed_session = session.model_copy(update={"intensity": intensity})
                transitions.append((
                    _replace_session(sessions, session.logical_session_id, changed_session),
                    CandidateOperation(kind=OperationKind.REDUCE_INTENSITY, logical_session_ids=(session.logical_session_id,), detail=intensity.value),
                ))

        for session in sessions:
            if session.logical_session_id not in editable_ids or session.logical_session_id in already_operated:
                continue
            if session.duration_minutes <= 20:
                continue
            reduced = max(20, int(session.duration_minutes * 0.75))
            attributes = dict(session.attributes)
            if isinstance(attributes.get("distance_km"), (int, float)):
                attributes["distance_km"] = attributes["distance_km"] * reduced / session.duration_minutes
            shortened_window = session.schedule_window.model_copy(update={
                "end_at": session.schedule_window.start_at + timedelta(minutes=reduced),
            })
            shortened = session.model_copy(update={"duration_minutes": reduced, "schedule_window": shortened_window, "attributes": attributes})
            transitions.append((
                _replace_session(sessions, session.logical_session_id, shortened),
                CandidateOperation(kind=OperationKind.REDUCE_DURATION, logical_session_ids=(session.logical_session_id,), detail=f"{session.duration_minutes}->{reduced} minutes"),
            ))

        for window in normalized.recovery_windows:
            recovery_id = f"recovery-{window.local_start_date.isoformat()}"
            if recovery_id in by_id or not _duration_fits(
                PlannedSession(logical_session_id=recovery_id, activity_type="recovery", schedule_window=window, intensity=Intensity.RECOVERY, duration_minutes=int((window.end_at-window.start_at).total_seconds() // 60)),
                window,
            ):
                continue
            recovery = PlannedSession(
                logical_session_id=recovery_id,
                activity_type="recovery",
                schedule_window=window,
                intensity=Intensity.RECOVERY,
                duration_minutes=int((window.end_at - window.start_at).total_seconds() // 60),
            )
            transitions.append((
                _canonical_sessions(sessions + (recovery,)),
                CandidateOperation(kind=OperationKind.ADD_RECOVERY, logical_session_ids=(recovery_id,), detail=window.local_start_date.isoformat()),
            ))

        # Swaps are atomic, so a legal exchange never emits an overlapping half-state.
        ordered = list(sessions)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                if left.logical_session_id not in editable_ids or right.logical_session_id not in editable_ids:
                    continue
                if left.logical_session_id in already_operated or right.logical_session_id in already_operated:
                    continue
                if not _duration_fits(left, right.schedule_window) or not _duration_fits(right, left.schedule_window):
                    continue
                left_new = left.model_copy(update={"schedule_window": right.schedule_window})
                right_new = right.model_copy(update={"schedule_window": left.schedule_window})
                swapped = _replace_session(sessions, left.logical_session_id, left_new)
                swapped = _replace_session(swapped, right.logical_session_id, right_new)
                transitions.append((swapped, CandidateOperation(
                    kind=OperationKind.SWAP_SESSIONS,
                    logical_session_ids=(left.logical_session_id, right.logical_session_id),
                )))

        for next_sessions, operation in transitions:
            next_changed = _changed_ids(source.current_plan, next_sessions)
            if len(next_changed) > max_changed_sessions or not _legal(next_sessions, normalized, constraints):
                continue
            _, digest = _content_identity(next_sessions)
            if digest in visited:
                continue
            visited.add(digest)
            next_operations = operations + (operation,)
            queue.append((next_sessions, next_operations))
            if not _requires_recovery_entry(next_sessions, normalized):
                candidate = _candidate(next_sessions, next_operations, source)
                found[digest] = candidate
            if len(found) >= max_candidates:
                break

    return tuple(sorted(found.values(), key=lambda c: c.content_hash))
