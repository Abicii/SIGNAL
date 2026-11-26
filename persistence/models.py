from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, JSON, SmallInteger, String, Text, Uuid, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


JsonType = JSON().with_variant(JSONB, "postgresql")


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("identity_subject", name="uq_users_identity_subject"),)

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    identity_subject: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class PlanVersionRecord(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("user_id", "version_number", name="uq_plan_versions_user_version"),
        CheckConstraint("status IN ('proposed','accepted','rejected','superseded')", name="ck_plan_version_status"),
        CheckConstraint("horizon_end >= horizon_start", name="ck_plan_version_horizon"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", name="fk_plan_versions_user"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("plan_versions.id", name="fk_plan_versions_parent"), nullable=True, index=True)
    decision_run_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("decision_runs.id", use_alter=True, name="fk_plan_version_decision_run"), nullable=True)
    trigger_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    horizon_start: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DecisionRunRecord(Base):
    __tablename__ = "decision_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','proposed','accepted','rejected','fallback','failed')", name="ck_decision_run_status"),
        CheckConstraint("user_outcome IN ('pending','accepted','rejected','edited','expired')", name="ck_decision_run_outcome"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", name="fk_decision_runs_user"), nullable=False, index=True)
    parent_plan_version_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("plan_versions.id", name="fk_decision_runs_parent"), nullable=True, index=True)
    result_plan_version_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("plan_versions.id", name="fk_decision_runs_result"), nullable=True, index=True)
    trigger_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    trigger_summary: Mapped[str | None] = mapped_column(String(240), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(60), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(60), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(60), nullable=False)
    input_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    conflicts: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False)
    candidate_scores: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False)
    adjudication: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    user_outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProfileRecord(Base):
    __tablename__ = "profiles"

    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", name="fk_profiles_user"), primary_key=True)
    current_plan_version_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("plan_versions.id", name="fk_profiles_current_plan"), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    preferred_units: Mapped[str] = mapped_column(String(16), nullable=False, default="metric")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlannedSessionRecord(Base):
    __tablename__ = "planned_sessions"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "logical_session_id", name="uq_planned_sessions_version_logical"),
        UniqueConstraint("plan_version_id", "local_date", "sequence", name="uq_planned_sessions_version_date_sequence"),
        CheckConstraint("schedule_end_at > schedule_start_at", name="ck_planned_session_window"),
        CheckConstraint("duration_minutes > 0", name="ck_planned_session_duration"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    logical_session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    plan_version_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("plan_versions.id", name="fk_planned_sessions_version"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", name="fk_planned_sessions_user"), nullable=False, index=True)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    schedule_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    activity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    intensity: Mapped[str] = mapped_column(String(24), nullable=False)
    attributes: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)


class PlanChangeRecord(Base):
    __tablename__ = "plan_changes"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "ordinal", name="uq_plan_changes_version_ordinal"),
        CheckConstraint("change_type IN ('added','removed','moved','modified','unchanged_context')", name="ck_plan_change_type"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    plan_version_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("plan_versions.id", name="fk_plan_changes_version"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", name="fk_plan_changes_user"), nullable=False, index=True)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    logical_session_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("planned_sessions.id", name="fk_plan_changes_session"), nullable=True, index=True)
    prior_session_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("planned_sessions.id", name="fk_plan_changes_prior_session"), nullable=True, index=True)
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    after_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
