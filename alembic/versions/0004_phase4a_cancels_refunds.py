"""phase4a cancels and refunds

Revision ID: 0004_phase4a_cancels_refunds
Revises: 0003_phase3a_holds_bookings
Create Date: 2026-09-06 00:48:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic. (Must be <= 32 chars for alembic_version table)
revision: str = "0004_phase4a_cancels_refunds"
down_revision: Union[str, None] = "0003_phase3a_holds_bookings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create Enums if Postgres
    if is_postgres:
        postgresql.ENUM("CONFIRMED", "CANCELLED", name="booking_item_status").create(bind, checkfirst=True)
        postgresql.ENUM("PENDING", "APPROVED", "PROCESSING", "COMPLETED", "FAILED", name="refund_status").create(bind, checkfirst=True)
        postgresql.ENUM("MONETARY", "CREDIT", "NONE", name="refund_type").create(bind, checkfirst=True)
        postgresql.ENUM(
            "CUSTOMER_CANCELLATION", "AIRLINE_CANCELLATION", "SCHEDULE_CHANGE", "BOOKING_CHANGE", "OTHER",
            name="refund_reason",
        ).create(bind, checkfirst=True)

    def enum_col(values: list[str], name: str):
        if is_postgres:
            return postgresql.ENUM(*values, name=name, create_type=False)
        return sa.Enum(*values, name=name, native_enum=False)

    booking_item_status_type = enum_col(["CONFIRMED", "CANCELLED"], "booking_item_status")
    refund_status_type = enum_col(["PENDING", "APPROVED", "PROCESSING", "COMPLETED", "FAILED"], "refund_status")
    refund_type_type = enum_col(["MONETARY", "CREDIT", "NONE"], "refund_type")
    refund_reason_type = enum_col(
        ["CUSTOMER_CANCELLATION", "AIRLINE_CANCELLATION", "SCHEDULE_CHANGE", "BOOKING_CHANGE", "OTHER"],
        "refund_reason",
    )
    flight_class_type_type = enum_col(["FIRST", "BUSINESS", "ECONOMY"], "flight_class_type")
    fare_type_type = enum_col(["BASIC", "FLEXIBLE"], "fare_type")

    # 2. Add status column to booking_items table
    op.add_column(
        "booking_items",
        sa.Column("status", booking_item_status_type, nullable=False, server_default="CONFIRMED"),
    )
    op.create_index(op.f("ix_booking_items_status"), "booking_items", ["status"], unique=False)

    # 3. Create refunds table
    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("booking_item_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("refund_type", refund_type_type, nullable=False),
        sa.Column("status", refund_status_type, nullable=False, server_default="PENDING"),
        sa.Column("reason", refund_reason_type, nullable=False, server_default="CUSTOMER_CANCELLATION"),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["booking_item_id"], ["booking_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("amount >= 0", name="ck_refunds_amount_non_negative"),
    )
    op.create_index(op.f("ix_refunds_booking_id"), "refunds", ["booking_id"], unique=False)
    op.create_index(op.f("ix_refunds_booking_item_id"), "refunds", ["booking_item_id"], unique=False)
    op.create_index(op.f("ix_refunds_status"), "refunds", ["status"], unique=False)
    op.create_index(op.f("ix_refunds_refund_type"), "refunds", ["refund_type"], unique=False)
    op.create_index(op.f("ix_refunds_reason"), "refunds", ["reason"], unique=False)
    op.create_index(op.f("ix_refunds_idempotency_key"), "refunds", ["idempotency_key"], unique=False)

    # 4. Create booking_changes table
    op.create_table(
        "booking_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("booking_item_id", sa.Uuid(), nullable=False),
        sa.Column("old_flight_id", sa.Uuid(), nullable=False),
        sa.Column("new_flight_id", sa.Uuid(), nullable=False),
        sa.Column("old_seat_id", sa.Uuid(), nullable=False),
        sa.Column("new_seat_id", sa.Uuid(), nullable=False),
        sa.Column("old_class_type", flight_class_type_type, nullable=False),
        sa.Column("new_class_type", flight_class_type_type, nullable=False),
        sa.Column("old_fare_type", fare_type_type, nullable=False),
        sa.Column("new_fare_type", fare_type_type, nullable=False),
        sa.Column("old_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("new_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("price_difference", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["booking_item_id"], ["booking_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["old_flight_id"], ["flights.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["new_flight_id"], ["flights.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["old_seat_id"], ["flight_seats.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["new_seat_id"], ["flight_seats.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_booking_changes_booking_id"), "booking_changes", ["booking_id"], unique=False)
    op.create_index(op.f("ix_booking_changes_booking_item_id"), "booking_changes", ["booking_item_id"], unique=False)
    op.create_index(op.f("ix_booking_changes_idempotency_key"), "booking_changes", ["idempotency_key"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.drop_index(op.f("ix_booking_changes_idempotency_key"), table_name="booking_changes")
    op.drop_index(op.f("ix_booking_changes_booking_item_id"), table_name="booking_changes")
    op.drop_index(op.f("ix_booking_changes_booking_id"), table_name="booking_changes")
    op.drop_table("booking_changes")

    op.drop_index(op.f("ix_refunds_idempotency_key"), table_name="refunds")
    op.drop_index(op.f("ix_refunds_reason"), table_name="refunds")
    op.drop_index(op.f("ix_refunds_refund_type"), table_name="refunds")
    op.drop_index(op.f("ix_refunds_status"), table_name="refunds")
    op.drop_index(op.f("ix_refunds_booking_item_id"), table_name="refunds")
    op.drop_index(op.f("ix_refunds_booking_id"), table_name="refunds")
    op.drop_table("refunds")

    op.drop_index(op.f("ix_booking_items_status"), table_name="booking_items")
    op.drop_column("booking_items", "status")

    if is_postgres:
        postgresql.ENUM(
            "CUSTOMER_CANCELLATION", "AIRLINE_CANCELLATION", "SCHEDULE_CHANGE", "BOOKING_CHANGE", "OTHER",
            name="refund_reason",
        ).drop(bind, checkfirst=True)
        postgresql.ENUM("MONETARY", "CREDIT", "NONE", name="refund_type").drop(bind, checkfirst=True)
        postgresql.ENUM("PENDING", "APPROVED", "PROCESSING", "COMPLETED", "FAILED", name="refund_status").drop(bind, checkfirst=True)
        postgresql.ENUM("CONFIRMED", "CANCELLED", name="booking_item_status").drop(bind, checkfirst=True)
