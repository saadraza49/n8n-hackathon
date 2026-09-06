"""phase7b policy knowledge base and rag infrastructure

Revision ID: 0008_phase7b_policy_kb_infra
Revises: 0007_phase7a_fraud_risk_infra
Create Date: 2026-09-06 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0008_phase7b_policy_kb_infra"
down_revision: Union[str, None] = "0007_phase7a_fraud_risk_infra"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create Enums if Postgres
    if is_postgres:
        postgresql.ENUM(
            "CANCELLATION", "REFUND", "REBOOKING", "FARE", "SEAT_HOLD",
            "WAITLIST", "SCHEDULE_CHANGE", "FLIGHT_CANCELLATION", "CHECK_IN", "GENERAL",
            name="policy_type",
        ).create(bind, checkfirst=True)

        postgresql.ENUM(
            "ACTIVE", "INACTIVE", "SUPERSEDED", "ARCHIVED",
            name="policy_document_status",
        ).create(bind, checkfirst=True)

        postgresql.ENUM(
            "PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED",
            name="policy_ingestion_status",
        ).create(bind, checkfirst=True)

    def enum_col(values: list[str], name: str):
        if is_postgres:
            return postgresql.ENUM(*values, name=name, create_type=False)
        return sa.Enum(*values, name=name, native_enum=False)

    policy_type_col = enum_col(
        ["CANCELLATION", "REFUND", "REBOOKING", "FARE", "SEAT_HOLD",
         "WAITLIST", "SCHEDULE_CHANGE", "FLIGHT_CANCELLATION", "CHECK_IN", "GENERAL"],
        "policy_type",
    )
    policy_doc_status_col = enum_col(["ACTIVE", "INACTIVE", "SUPERSEDED", "ARCHIVED"], "policy_document_status")
    policy_ingest_status_col = enum_col(["PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"], "policy_ingestion_status")
    json_col = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()

    # 2. Create policy_documents table
    op.create_table(
        "policy_documents",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("document_name", sa.String(255), nullable=False),
        sa.Column("policy_type", policy_type_col, nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("source_url", sa.String(1024), nullable=True),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("status", policy_doc_status_col, nullable=False, server_default="ACTIVE"),
        sa.Column("ingestion_status", policy_ingest_status_col, nullable=False, server_default="PENDING"),
        sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_length", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_info", json_col, nullable=False, server_default="{}" if is_postgres else None),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_name", "version", name="uq_policy_documents_name_version"),
    )

    op.create_index("ix_policy_documents_document_name", "policy_documents", ["document_name"])
    op.create_index("ix_policy_documents_policy_type", "policy_documents", ["policy_type"])
    op.create_index("ix_policy_documents_version", "policy_documents", ["version"])
    op.create_index("ix_policy_documents_document_hash", "policy_documents", ["document_hash"])
    op.create_index("ix_policy_documents_status", "policy_documents", ["status"])
    op.create_index("ix_policy_documents_ingestion_status", "policy_documents", ["ingestion_status"])
    op.create_index("ix_policy_documents_effective_from", "policy_documents", ["effective_from"])
    op.create_index("ix_policy_documents_created_by", "policy_documents", ["created_by"])
    op.create_index("ix_policy_documents_created_at", "policy_documents", ["created_at"])

    # 3. Add fare_rule_snapshot column to booking_items
    op.add_column("booking_items", sa.Column("fare_rule_snapshot", json_col, nullable=True))

    # 4. Enable RLS on policy_documents if Postgres
    if is_postgres:
        op.execute("ALTER TABLE policy_documents ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Remove fare_rule_snapshot column from booking_items
    op.drop_column("booking_items", "fare_rule_snapshot")

    # Drop policy_documents table and indexes
    op.drop_table("policy_documents")

    # Drop Enums if Postgres
    if is_postgres:
        op.execute("DROP TYPE IF EXISTS policy_ingestion_status CASCADE;")
        op.execute("DROP TYPE IF EXISTS policy_document_status CASCADE;")
        op.execute("DROP TYPE IF EXISTS policy_type CASCADE;")
