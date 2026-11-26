from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, event, func, inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from service.models import ReplanResult
from signal_engine.models import PlannedSession, ScheduleWindow

from .models import (
    Base, DecisionRunRecord, PlanChangeRecord, PlannedSessionRecord,
    PlanVersionRecord, ProfileRecord, UserRecord,
)


SAMPLE_USER_ID = "00000000-0000-0000-0000-000000000001"


class PersistenceError(RuntimeError):
    pass


class ProposalNotFound(PersistenceError):
    pass


class StaleProposal(PersistenceError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _db_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SignalRepository:
    """Small SQLAlchemy repository; planner and scoring remain outside storage."""

    def __init__(self, url: str, *, engine: Engine | None = None) -> None:
        if engine is not None:
            self.engine = engine
        elif url.startswith("sqlite") and (url in {"sqlite://", "sqlite:///:memory:"}):
            self.engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
        else:
            self.engine = create_engine(url, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _enable_sqlite_foreign_keys(connection, _record) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def from_environment(cls) -> "SignalRepository":
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise PersistenceError("DATABASE_URL must point to the SIGNAL PostgreSQL database")
        return cls(url)

    def initialize(self, *, seed_sample: bool = True) -> None:
        tables = set(inspect(self.engine).get_table_names())
        missing = set(Base.metadata.tables) - tables
        if missing or "alembic_version" not in tables:
            names = ", ".join(sorted(missing or {"alembic_version"}))
            raise PersistenceError(f"SIGNAL schema is not migrated (missing {names}); run `alembic upgrade head`")
        if not seed_sample:
            return
        from app.sample_data import PROFILE_TIMEZONE, WEEK_END, WEEK_START, canonical_plan

        now = datetime.now(timezone.utc)
        with self._sessions.begin() as db:
            user = db.get(UserRecord, SAMPLE_USER_ID)
            if user is None:
                db.add(UserRecord(
                    id=SAMPLE_USER_ID,
                    identity_subject="signal-local-sample-user",
                    created_at=now,
                    status="active",
                ))
                db.flush()
                profile = ProfileRecord(
                    user_id=SAMPLE_USER_ID,
                    current_plan_version_id=None,
                    timezone=PROFILE_TIMEZONE,
                    preferred_units="metric",
                    created_at=now,
                    updated_at=now,
                )
                db.add(profile)
                initial = PlanVersionRecord(
                    id=str(uuid4()), user_id=SAMPLE_USER_ID, version_number=1,
                    parent_version_id=None, decision_run_id=None, trigger_kind="initial",
                    trigger_ref=None, horizon_start=WEEK_START, horizon_end=WEEK_END,
                    status="accepted", created_at=now, accepted_at=now,
                )
                db.add(initial)
                db.flush()
                self._insert_sessions(db, SAMPLE_USER_ID, initial.id, canonical_plan())
                profile.current_plan_version_id = initial.id

    def dispose(self) -> None:
        self.engine.dispose()

    def current_plan(self, user_id: str = SAMPLE_USER_ID) -> tuple[tuple[PlannedSession, ...], str, str]:
        with self._sessions() as db:
            profile = db.get(ProfileRecord, user_id)
            if profile is None or profile.current_plan_version_id is None:
                raise PersistenceError("No accepted current plan is available")
            version = db.get(PlanVersionRecord, profile.current_plan_version_id)
            rows = db.scalars(select(PlannedSessionRecord).where(
                PlannedSessionRecord.plan_version_id == version.id,
                PlannedSessionRecord.user_id == user_id,
            ).order_by(PlannedSessionRecord.local_date, PlannedSessionRecord.sequence)).all()
            return tuple(self._to_domain(row) for row in rows), version.id, profile.timezone

    def proposal_pending(self, user_id: str = SAMPLE_USER_ID) -> bool:
        with self._sessions() as db:
            return db.scalar(select(DecisionRunRecord.id).where(
                DecisionRunRecord.user_id == user_id,
                DecisionRunRecord.status == "proposed",
                DecisionRunRecord.user_outcome == "pending",
            ).limit(1)) is not None

    def create_proposal(
        self,
        result: ReplanResult,
        *,
        expected_parent_id: str,
        trigger_kind: str,
    ) -> ReplanResult:
        if result.proposed_plan is None or not result.validation.valid or result.selected_candidate is None:
            raise PersistenceError("Only a validated selected plan can be persisted as a proposal")
        now = datetime.now(timezone.utc)
        decision_id = str(uuid4())
        plan_id = str(uuid4())
        with self._sessions.begin() as db:
            profile = db.scalar(select(ProfileRecord).where(ProfileRecord.user_id == SAMPLE_USER_ID).with_for_update())
            if profile is None or profile.current_plan_version_id != expected_parent_id:
                raise StaleProposal("The current plan changed while this proposal was being prepared")
            parent = db.get(PlanVersionRecord, expected_parent_id)
            if parent is None or parent.user_id != SAMPLE_USER_ID:
                raise PersistenceError("Proposal parent plan does not belong to this profile")
            next_number = db.scalar(select(func.max(PlanVersionRecord.version_number)).where(
                PlanVersionRecord.user_id == SAMPLE_USER_ID,
            )) or 0
            snapshot = result.input_snapshot.model_dump(mode="json") if result.input_snapshot else {}
            snapshot["snapshot_schema_version"] = "1"
            candidates_by_id = {item.candidate_id: item for item in result.candidate_plans}
            score_rows = []
            for score in result.candidate_scores:
                candidate = candidates_by_id.get(score.candidate_id)
                score_rows.append({
                    "score": score.model_dump(mode="json"),
                    "candidate": candidate.model_dump(mode="json") if candidate else None,
                })
            event = result.input_snapshot.life_event if result.input_snapshot else None
            summary = None if event is None else f"{event.kind}; {event.starts_at.isoformat()} to {event.ends_at.isoformat()}"
            run = DecisionRunRecord(
                id=decision_id, user_id=SAMPLE_USER_ID,
                parent_plan_version_id=expected_parent_id, result_plan_version_id=None,
                trigger_kind=trigger_kind, trigger_ref=None, trigger_summary=summary,
                started_at=now, completed_at=now, status="proposed",
                engine_version=result.engine_version, policy_version=result.policy_version,
                validator_version=result.validation.validator_version,
                input_schema_version="1", input_snapshot=snapshot,
                conflicts=[item.model_dump(mode="json") for item in result.conflicts],
                candidate_scores=score_rows,
                adjudication={
                    "selected_candidate_id": result.selected_candidate.candidate_id,
                    "rationale_codes": list(result.reason_codes),
                    **result.adjudication.model_dump(mode="json"),
                },
                validation=result.validation.model_dump(mode="json"),
                user_outcome="pending", outcome_at=None,
            )
            db.add(run)
            db.flush()
            version = PlanVersionRecord(
                id=plan_id, user_id=SAMPLE_USER_ID, version_number=next_number + 1,
                parent_version_id=expected_parent_id, decision_run_id=decision_id,
                trigger_kind=trigger_kind, trigger_ref=None,
                horizon_start=result.input_snapshot.horizon_start,
                horizon_end=result.input_snapshot.horizon_end,
                status="proposed", created_at=now, accepted_at=None,
            )
            db.add(version)
            db.flush()
            run.result_plan_version_id = plan_id
            db.flush()
            new_session_rows = self._insert_sessions(db, SAMPLE_USER_ID, plan_id, result.proposed_plan)
            old_session_rows = db.scalars(select(PlannedSessionRecord).where(
                PlannedSessionRecord.plan_version_id == expected_parent_id,
                PlannedSessionRecord.user_id == SAMPLE_USER_ID,
            )).all()
            old_by_id = {row.logical_session_id: row for row in old_session_rows}
            self._insert_changes(db, result, SAMPLE_USER_ID, plan_id, result.original_plan,
                                 result.proposed_plan, new_session_rows, old_by_id)
        return result.model_copy(update={"decision_id": decision_id})

    def record_decision(self, result: ReplanResult, *, parent_version_id: str, trigger_kind: str) -> ReplanResult:
        """Keep failed/no-candidate replan provenance without inventing a plan version."""
        now = datetime.now(timezone.utc)
        decision_id = str(uuid4())
        snapshot = result.input_snapshot.model_dump(mode="json") if result.input_snapshot else {}
        snapshot["snapshot_schema_version"] = "1"
        candidates = {item.candidate_id: item for item in result.candidate_plans}
        scores = [{
            "score": score.model_dump(mode="json"),
            "candidate": candidates[score.candidate_id].model_dump(mode="json") if score.candidate_id in candidates else None,
        } for score in result.candidate_scores]
        with self._sessions.begin() as db:
            profile = db.scalar(select(ProfileRecord).where(ProfileRecord.user_id == SAMPLE_USER_ID).with_for_update())
            if profile is None or profile.current_plan_version_id != parent_version_id:
                raise StaleProposal("The current plan changed while this decision was being prepared")
            event = result.input_snapshot.life_event if result.input_snapshot else None
            db.add(DecisionRunRecord(
                id=decision_id, user_id=SAMPLE_USER_ID,
                parent_plan_version_id=parent_version_id, result_plan_version_id=None,
                trigger_kind=trigger_kind, trigger_ref=None,
                trigger_summary=(f"{event.kind}; {event.starts_at.isoformat()} to {event.ends_at.isoformat()}" if event else None),
                started_at=now, completed_at=now,
                status="failed", engine_version=result.engine_version,
                policy_version=result.policy_version, validator_version=result.validation.validator_version,
                input_schema_version="1", input_snapshot=snapshot,
                conflicts=[item.model_dump(mode="json") for item in result.conflicts],
                candidate_scores=scores, adjudication=None,
                validation=result.validation.model_dump(mode="json"),
                user_outcome="pending", outcome_at=None,
            ))
        return result.model_copy(update={"decision_id": decision_id})

    def decide_proposal(self, decision_id: str | None, action: str, user_id: str = SAMPLE_USER_ID) -> tuple[str, tuple[PlannedSession, ...]]:
        if action not in {"accept", "reject"}:
            raise ValueError("action must be accept or reject")
        now = datetime.now(timezone.utc)
        stale = False
        result_plan: tuple[PlannedSession, ...] = ()
        with self._sessions.begin() as db:
            if decision_id is None:
                run = db.scalar(select(DecisionRunRecord).where(
                    DecisionRunRecord.user_id == user_id,
                    DecisionRunRecord.status == "proposed",
                    DecisionRunRecord.user_outcome == "pending",
                ).order_by(DecisionRunRecord.started_at.desc(), DecisionRunRecord.id.desc()).with_for_update().limit(1))
            else:
                run = db.scalar(select(DecisionRunRecord).where(
                    DecisionRunRecord.id == decision_id,
                    DecisionRunRecord.user_id == user_id,
                ).with_for_update())
            if run is None:
                raise ProposalNotFound("There is no proposal awaiting a decision")
            if run.status != "proposed" or run.user_outcome != "pending" or run.result_plan_version_id is None:
                raise ProposalNotFound("This proposal has already been decided")
            proposal = db.get(PlanVersionRecord, run.result_plan_version_id)
            profile = db.scalar(select(ProfileRecord).where(ProfileRecord.user_id == user_id).with_for_update())
            if proposal is None or profile is None:
                raise ProposalNotFound("The proposal or profile no longer exists")
            if action == "reject":
                proposal.status = "rejected"
                run.status = "rejected"
                run.user_outcome = "rejected"
                run.outcome_at = now
            else:
                if run.validation is None or not run.validation.get("valid"):
                    raise PersistenceError("An unvalidated proposal cannot be accepted")
                expected_parent = run.parent_plan_version_id
                parent_match = profile.current_plan_version_id == expected_parent
                if parent_match:
                    condition = ProfileRecord.current_plan_version_id == expected_parent
                    changed = db.execute(update(ProfileRecord).where(
                        ProfileRecord.user_id == user_id, condition,
                    ).values(current_plan_version_id=proposal.id, updated_at=now))
                    parent_match = changed.rowcount == 1
                if not parent_match:
                    proposal.status = "superseded"
                    run.user_outcome = "expired"
                    run.outcome_at = now
                    stale = True
                else:
                    if expected_parent is not None:
                        previous = db.get(PlanVersionRecord, expected_parent)
                        if previous is not None:
                            previous.status = "superseded"
                    proposal.status = "accepted"
                    proposal.accepted_at = now
                    run.status = "accepted"
                    run.user_outcome = "accepted"
                    run.outcome_at = now
            rows = db.scalars(select(PlannedSessionRecord).where(
                PlannedSessionRecord.plan_version_id == profile.current_plan_version_id,
                PlannedSessionRecord.user_id == user_id,
            ).order_by(PlannedSessionRecord.local_date, PlannedSessionRecord.sequence)).all()
            result_plan = tuple(self._to_domain(row) for row in rows)
        if stale:
            raise StaleProposal("This proposal was based on an older plan and can no longer be accepted")
        status = "accepted" if action == "accept" else "rejected"
        return status, result_plan

    def get_plan_version(self, version_id: str) -> dict | None:
        with self._sessions() as db:
            row = db.get(PlanVersionRecord, version_id)
            if row is None:
                return None
            sessions = db.scalars(select(PlannedSessionRecord).where(PlannedSessionRecord.plan_version_id == version_id)).all()
            return {"id": row.id, "status": row.status, "sessions": tuple(self._to_domain(item) for item in sessions)}

    def get_decision_run(self, decision_id: str) -> DecisionRunRecord | None:
        with self._sessions() as db:
            return db.get(DecisionRunRecord, decision_id)

    def plan_changes(self, plan_version_id: str) -> tuple[PlanChangeRecord, ...]:
        with self._sessions() as db:
            return tuple(db.scalars(select(PlanChangeRecord).where(
                PlanChangeRecord.plan_version_id == plan_version_id,
            ).order_by(PlanChangeRecord.ordinal)).all())

    def _insert_sessions(self, db: Session, user_id: str, plan_id: str, sessions: tuple[PlannedSession, ...]) -> dict[str, PlannedSessionRecord]:
        ordered = sorted(sessions, key=lambda item: (item.local_date, item.schedule_window.start_at, item.logical_session_id))
        counts: dict[str, int] = {}
        rows = {}
        for item in ordered:
            counts[item.local_date.isoformat()] = counts.get(item.local_date.isoformat(), 0) + 1
            row = PlannedSessionRecord(
                id=str(uuid4()), logical_session_id=item.logical_session_id,
                plan_version_id=plan_id, user_id=user_id, local_date=item.local_date,
                schedule_start_at=_utc(item.schedule_window.start_at),
                schedule_end_at=_utc(item.schedule_window.end_at),
                schedule_timezone=item.schedule_window.timezone,
                sequence=counts[item.local_date.isoformat()], activity_type=item.activity_type,
                duration_minutes=item.duration_minutes, intensity=item.intensity.value,
                attributes=item.attributes or None,
            )
            db.add(row)
            rows[item.logical_session_id] = row
        db.flush()
        return rows

    def _insert_changes(self, db: Session, result: ReplanResult, user_id: str, plan_id: str,
                        before: tuple[PlannedSession, ...], after: tuple[PlannedSession, ...],
                        child_rows: dict[str, PlannedSessionRecord],
                        prior_rows: dict[str, PlannedSessionRecord]) -> None:
        old_by_id = {item.logical_session_id: item for item in before}
        new_by_id = {item.logical_session_id: item for item in after}
        supported_codes = result.reason_codes
        for ordinal, change in enumerate(result.changes, start=1):
            previous = old_by_id.get(change.logical_session_id)
            current = new_by_id.get(change.logical_session_id)
            reason = "REDUCE_RECOVERY_CONFLICT" if (
                current is not None and current.activity_type.lower() == "recovery"
                and "REDUCE_RECOVERY_CONFLICT" in supported_codes
            ) else next(iter(supported_codes), None)
            if reason is None:
                raise PersistenceError(f"Plan change {change.logical_session_id} has no supported reason code")
            db.add(PlanChangeRecord(
                id=str(uuid4()), plan_version_id=plan_id, user_id=user_id,
                change_type=change.change_type, logical_session_id=change.logical_session_id,
                session_id=child_rows[current.logical_session_id].id if current else None,
                prior_session_id=prior_rows[previous.logical_session_id].id if previous else None,
                before_snapshot=previous.model_dump(mode="json") if previous else None,
                after_snapshot=current.model_dump(mode="json") if current else None,
                reason_code=reason, explanation=None, ordinal=ordinal,
            ))

    @staticmethod
    def _to_domain(row: PlannedSessionRecord) -> PlannedSession:
        start = _db_utc(row.schedule_start_at)
        end = _db_utc(row.schedule_end_at)
        return PlannedSession.model_validate({
            "logical_session_id": row.logical_session_id,
            "activity_type": row.activity_type,
            "schedule_window": {
                "start_at": start,
                "end_at": end,
                "timezone": row.schedule_timezone,
            },
            "intensity": row.intensity,
            "duration_minutes": row.duration_minutes,
            "attributes": row.attributes or {},
        })
