"""phase5a waitlist and standby system

Revision ID: 0005_phase5a_waitlist_standby
Revises: 0004_phase4a_cancels_refunds
Create Date: 2026-09-06 01:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic. (Must be <= 32 chars for alembic_version table)
revision: str = "0005_phase5a_waitlist_standby"
down_revision: Union[str, None] = "0004_phase4a_cancels_refunds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create Enums if Postgres
    if is_postgres:
        postgresql.ENUM(
            "WAITING", "PROMOTED", "CLAIMED", "CONVERTED", "EXPIRED", "CANCELLED",
            name="waitlist_status",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "WAITLIST", "STANDBY",
            name="waitlist_entry_type",
        ).create(bind, checkfirst=True)

    def enum_col(values: list[str], name: str):
        if is_postgres:
            return postgresql.ENUM(*values, name=name, create_type=False)
        return sa.Enum(*values, name=name, native_enum=False)

    waitlist_status_type = enum_col(
        ["WAITING", "PROMOTED", "CLAIMED", "CONVERTED", "EXPIRED", "CANCELLED"],
        "waitlist_status",
    )
    waitlist_entry_type_type = enum_col(["WAITLIST", "STANDBY"], "waitlist_entry_type")
    flight_class_type_type = enum_col(["FIRST", "BUSINESS", "ECONOMY"], "flight_class_type")

    # 2. Create waitlist_entries table
    op.create_table(
        "waitlist_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("flight_id", sa.Uuid(), nullable=False),
        sa.Column("passenger_id", sa.Uuid(), nullable=False),
        sa.Column("class_type", flight_class_type_type, nullable=False),
        sa.Column("entry_type", waitlist_entry_type_type, nullable=False, server_default="WAITLIST"),
        sa.Column("status", waitlist_status_type, nullable=False, server_default="WAITING"),
        sa.Column("priority_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_seat_id", sa.Uuid(), nullable=True),
        sa.Column("booking_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["passenger_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["promoted_seat_id"], ["flight_seats.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    # 3. Create Indexes
    op.create_index(op.f("ix_waitlist_entries_flight_id"), "waitlist_entries", ["flight_id"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_passenger_id"), "waitlist_entries", ["passenger_id"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_class_type"), "waitlist_entries", ["class_type"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_entry_type"), "waitlist_entries", ["entry_type"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_status"), "waitlist_entries", ["status"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_priority_score"), "waitlist_entries", ["priority_score"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_joined_at"), "waitlist_entries", ["joined_at"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_claim_deadline"), "waitlist_entries", ["claim_deadline"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_promoted_seat_id"), "waitlist_entries", ["promoted_seat_id"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_booking_id"), "waitlist_entries", ["booking_id"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_idempotency_key"), "waitlist_entries", ["idempotency_key"], unique=False)

    # Composite index for priority queue ordering and promotion
    op.create_index(
        "ix_waitlist_queue",
        "waitlist_entries",
        ["flight_id", "class_type", "status", "priority_score", "joined_at", "id"],
        unique=False,
    )
    # Composite index for passenger status lookup
    op.create_index(
        "ix_waitlist_passenger_status",
        "waitlist_entries",
        ["passenger_id", "status"],
        unique=False,
    )
    # Composite index for deadline sweep
    op.create_index(
        "ix_waitlist_claim_deadline",
        "waitlist_entries",
        ["status", "claim_deadline"],
        unique=False,
    )

    # 4. Partial unique index: A passenger can have only one active entry per (flight_id, class_type)
    if is_postgres:
        op.execute(
            """
            CREATE UNIQUE INDEX uq_waitlist_active_passenger
            ON waitlist_entries (passenger_id, flight_id, class_type)
            WHERE status IN ('WAITING', 'PROMOTED', 'CLAIMED');
            """
        )
    else:
        op.create_index(
            "uq_waitlist_active_passenger",
            "waitlist_entries",
            ["passenger_id", "flight_id", "class_type"],
            unique=True,
            postgresql_where=sa.text("status IN ('WAITING', 'PROMOTED', 'CLAIMED')"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Drop indexes
    op.drop_index("uq_waitlist_active_passenger", table_name="waitlist_entries")
    op.drop_index("ix_waitlist_claim_deadline", table_name="waitlist_entries")
    op.drop_index("ix_waitlist_passenger_status", table_name="waitlist_entries")
    op.drop_index("ix_waitlist_queue", table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_idempotency_key"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_booking_id"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_promoted_seat_id"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_claim_deadline"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_joined_at"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_priority_score"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_status"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_entry_type"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_class_type"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_passenger_id"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_flight_id"), table_name="waitlist_entries")

    # Drop table
    op.drop_table("waitlist_entries")

    # Drop Enums if Postgres
    if is_postgres:
        postgresql.ENUM("WAITLIST", "STANDBY", name="waitlist_entry_type").drop(bind, checkfirst=True)
        postgresql.ENUM("WAITING", "PROMOTED", "CLAIMED", "CONVERTED", "EXPIRED", "CANCELLED", name="waitlist_status").drop(bind, checkfirst=True)
