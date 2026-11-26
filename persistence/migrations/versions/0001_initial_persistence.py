"""Initial SIGNAL plan and decision persistence schema.

Revision ID: 0001_initial_persistence
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_initial_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("identity_subject", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("identity_subject", name="uq_users_identity_subject"),
    )
    op.create_table(
        "plan_versions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("decision_run_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("trigger_kind", sa.String(length=40), nullable=False),
        sa.Column("trigger_ref", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("horizon_start", sa.Date(), nullable=False),
        sa.Column("horizon_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("horizon_end >= horizon_start", name="ck_plan_version_horizon"),
        sa.CheckConstraint("status IN ('proposed','accepted','rejected','superseded')", name="ck_plan_version_status"),
        sa.ForeignKeyConstraint(["parent_version_id"], ["plan_versions.id"], name="fk_plan_versions_parent"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_plan_versions_user"),
        sa.PrimaryKeyConstraint("id", name="pk_plan_versions"),
        sa.UniqueConstraint("user_id", "version_number", name="uq_plan_versions_user_version"),
    )
    op.create_index("ix_plan_versions_user_id", "plan_versions", ["user_id"])
    op.create_index("ix_plan_versions_parent_version_id", "plan_versions", ["parent_version_id"])

    op.create_table(
        "decision_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("parent_plan_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("result_plan_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("trigger_kind", sa.String(length=40), nullable=False),
        sa.Column("trigger_ref", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("trigger_summary", sa.String(length=240), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("engine_version", sa.String(length=60), nullable=False),
        sa.Column("policy_version", sa.String(length=60), nullable=False),
        sa.Column("validator_version", sa.String(length=60), nullable=False),
        sa.Column("input_schema_version", sa.String(length=32), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("conflicts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidate_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("adjudication", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("user_outcome", sa.String(length=24), nullable=False),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('running','proposed','accepted','rejected','fallback','failed')", name="ck_decision_run_status"),
        sa.CheckConstraint("user_outcome IN ('pending','accepted','rejected','edited','expired')", name="ck_decision_run_outcome"),
        sa.ForeignKeyConstraint(["parent_plan_version_id"], ["plan_versions.id"], name="fk_decision_runs_parent"),
        sa.ForeignKeyConstraint(["result_plan_version_id"], ["plan_versions.id"], name="fk_decision_runs_result"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_decision_runs_user"),
        sa.PrimaryKeyConstraint("id", name="pk_decision_runs"),
    )
    for column in ("user_id", "parent_plan_version_id", "result_plan_version_id"):
        op.create_index(f"ix_decision_runs_{column}", "decision_runs", [column])
    op.create_foreign_key(
        "fk_plan_version_decision_run", "plan_versions", "decision_runs",
        ["decision_run_id"], ["id"],
    )

    op.create_table(
        "profiles",
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("current_plan_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("preferred_units", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["current_plan_version_id"], ["plan_versions.id"], name="fk_profiles_current_plan"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_profiles_user"),
        sa.PrimaryKeyConstraint("user_id", name="pk_profiles"),
    )
    op.create_table(
        "planned_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("logical_session_id", sa.String(length=120), nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("schedule_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column("activity_type", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("intensity", sa.String(length=24), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("duration_minutes > 0", name="ck_planned_session_duration"),
        sa.CheckConstraint("schedule_end_at > schedule_start_at", name="ck_planned_session_window"),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], name="fk_planned_sessions_version"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_planned_sessions_user"),
        sa.PrimaryKeyConstraint("id", name="pk_planned_sessions"),
        sa.UniqueConstraint("plan_version_id", "local_date", "sequence", name="uq_planned_sessions_version_date_sequence"),
        sa.UniqueConstraint("plan_version_id", "logical_session_id", name="uq_planned_sessions_version_logical"),
    )
    for column in ("logical_session_id", "plan_version_id", "user_id"):
        op.create_index(f"ix_planned_sessions_{column}", "planned_sessions", [column])

    op.create_table(
        "plan_changes",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("logical_session_id", sa.String(length=120), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("prior_session_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("change_type IN ('added','removed','moved','modified','unchanged_context')", name="ck_plan_change_type"),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], name="fk_plan_changes_version"),
        sa.ForeignKeyConstraint(["prior_session_id"], ["planned_sessions.id"], name="fk_plan_changes_prior_session"),
        sa.ForeignKeyConstraint(["session_id"], ["planned_sessions.id"], name="fk_plan_changes_session"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_plan_changes_user"),
        sa.PrimaryKeyConstraint("id", name="pk_plan_changes"),
        sa.UniqueConstraint("plan_version_id", "ordinal", name="uq_plan_changes_version_ordinal"),
    )
    for column in ("plan_version_id", "user_id", "logical_session_id", "session_id", "prior_session_id"):
        op.create_index(f"ix_plan_changes_{column}", "plan_changes", [column])


def downgrade() -> None:
    op.drop_index("ix_plan_changes_prior_session_id", table_name="plan_changes")
    op.drop_index("ix_plan_changes_session_id", table_name="plan_changes")
    op.drop_index("ix_plan_changes_logical_session_id", table_name="plan_changes")
    op.drop_index("ix_plan_changes_user_id", table_name="plan_changes")
    op.drop_index("ix_plan_changes_plan_version_id", table_name="plan_changes")
    op.drop_table("plan_changes")
    op.drop_index("ix_planned_sessions_user_id", table_name="planned_sessions")
    op.drop_index("ix_planned_sessions_plan_version_id", table_name="planned_sessions")
    op.drop_index("ix_planned_sessions_logical_session_id", table_name="planned_sessions")
    op.drop_table("planned_sessions")
    op.drop_table("profiles")
    op.drop_constraint("fk_plan_version_decision_run", "plan_versions", type_="foreignkey")
    for column in ("result_plan_version_id", "parent_plan_version_id", "user_id"):
        op.drop_index(f"ix_decision_runs_{column}", table_name="decision_runs")
    op.drop_table("decision_runs")
    op.drop_index("ix_plan_versions_parent_version_id", table_name="plan_versions")
    op.drop_index("ix_plan_versions_user_id", table_name="plan_versions")
    op.drop_table("plan_versions")
    op.drop_table("users")
