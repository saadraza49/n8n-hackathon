"""phase7a fraud risk and policy infrastructure

Revision ID: 0007_phase7a_fraud_risk_infra
Revises: 0006_phase6a_automation_infra
Create Date: 2026-09-06 06:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic. (Must be <= 32 chars for alembic_version table)
revision: str = "0007_phase7a_fraud_risk_infra"
down_revision: Union[str, None] = "0006_phase6a_automation_infra"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create Enums if Postgres
    if is_postgres:
        postgresql.ENUM(
            "LOW", "MEDIUM", "HIGH", "CRITICAL",
            name="risk_level",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "ALLOW", "REVIEW", "BLOCK",
            name="fraud_decision",
        ).create(bind, checkfirst=True)

    def enum_col(values: list[str], name: str):
        if is_postgres:
            return postgresql.ENUM(*values, name=name, create_type=False)
        return sa.Enum(*values, name=name, native_enum=False)

    risk_level_col = enum_col(["LOW", "MEDIUM", "HIGH", "CRITICAL"], "risk_level")
    fraud_decision_col = enum_col(["ALLOW", "REVIEW", "BLOCK"], "fraud_decision")
    reasons_col = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()

    # 2. Create fraud_evaluations table
    op.create_table(
        "fraud_evaluations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("booking_id", sa.Uuid(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("risk_level", risk_level_col, nullable=False),
        sa.Column("decision", fraud_decision_col, nullable=False),
        sa.Column("reasons", reasons_col, nullable=False),
        sa.Column("evaluator", sa.String(100), nullable=False, server_default="RULE_ENGINE_V1"),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_fraud_evaluations_score_range"),
    )

    op.create_index("ix_fraud_evaluations_booking_id", "fraud_evaluations", ["booking_id"])
    op.create_index("ix_fraud_evaluations_user_id", "fraud_evaluations", ["user_id"])
    op.create_index("ix_fraud_evaluations_idempotency_key", "fraud_evaluations", ["idempotency_key"])
    op.create_index("ix_fraud_evaluations_evaluated_at", "fraud_evaluations", ["evaluated_at"])
    op.create_index("ix_fraud_evaluations_risk_level", "fraud_evaluations", ["risk_level"])
    op.create_index("ix_fraud_evaluations_decision", "fraud_evaluations", ["decision"])
    op.create_index("ix_fraud_evaluations_booking_evaluated", "fraud_evaluations", ["booking_id", "evaluated_at"])

    # 3. Create risk_signals child table
    op.create_table(
        "risk_signals",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("evaluation_id", sa.Uuid(as_uuid=True), sa.ForeignKey("fraud_evaluations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal_code", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(50), nullable=False),
        sa.Column("score_contribution", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("evidence", reasons_col, nullable=True),
        sa.Column("metadata", reasons_col, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index("ix_risk_signals_evaluation_id", "risk_signals", ["evaluation_id"])
    op.create_index("ix_risk_signals_signal_code", "risk_signals", ["signal_code"])

    if is_postgres:
        op.execute("ALTER TABLE fraud_evaluations ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE risk_signals ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.drop_index("ix_fraud_evaluations_booking_evaluated", table_name="fraud_evaluations")
    op.drop_index("ix_fraud_evaluations_decision", table_name="fraud_evaluations")
    op.drop_index("ix_fraud_evaluations_risk_level", table_name="fraud_evaluations")
    op.drop_index("ix_fraud_evaluations_evaluated_at", table_name="fraud_evaluations")
    op.drop_index("ix_fraud_evaluations_user_id", table_name="fraud_evaluations")
    op.drop_index("ix_fraud_evaluations_booking_id", table_name="fraud_evaluations")

    op.drop_table("fraud_evaluations")

    if is_postgres:
        sa.Enum(name="fraud_decision").drop(bind, checkfirst=True)
        sa.Enum(name="risk_level").drop(bind, checkfirst=True)
