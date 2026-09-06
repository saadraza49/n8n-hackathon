"""phase3a holds and bookings

Revision ID: 0003_phase3a_holds_bookings
Revises: 0002_phase1a_flight_foundation
Create Date: 2026-09-06 00:18:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic. (Must be <= 32 chars for alembic_version table)
revision: str = "0003_phase3a_holds_bookings"
down_revision: Union[str, None] = "0002_phase1a_flight_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create booking_status enum if Postgres
    if is_postgres:
        postgresql.ENUM(
            "PENDING", "CONFIRMED", "CANCELLED", "EXPIRED",
            name="booking_status",
        ).create(bind, checkfirst=True)

    def enum_col(values: list[str], name: str):
        if is_postgres:
            return postgresql.ENUM(*values, name=name, create_type=False)
        return sa.Enum(*values, name=name, native_enum=False)

    booking_status_type = enum_col(["PENDING", "CONFIRMED", "CANCELLED", "EXPIRED"], "booking_status")
    flight_class_type_type = enum_col(["FIRST", "BUSINESS", "ECONOMY"], "flight_class_type")
    fare_type_type = enum_col(["BASIC", "FLEXIBLE"], "fare_type")

    # 2. Create bookings table
    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_reference", sa.String(10), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("flight_id", sa.Uuid(), nullable=False),
        sa.Column("status", booking_status_type, nullable=False, server_default="PENDING"),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_reference", name="uq_bookings_booking_reference"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_bookings_user_idempotency"),
        sa.CheckConstraint("total_amount >= 0", name="ck_bookings_total_amount_non_negative"),
    )
    op.create_index(op.f("ix_bookings_booking_reference"), "bookings", ["booking_reference"], unique=False)
    op.create_index(op.f("ix_bookings_user_id"), "bookings", ["user_id"], unique=False)
    op.create_index(op.f("ix_bookings_flight_id"), "bookings", ["flight_id"], unique=False)
    op.create_index(op.f("ix_bookings_status"), "bookings", ["status"], unique=False)
    op.create_index(op.f("ix_bookings_idempotency_key"), "bookings", ["idempotency_key"], unique=False)
    op.create_index(op.f("ix_bookings_hold_expires_at"), "bookings", ["hold_expires_at"], unique=False)

    # 3. Create booking_items table
    op.create_table(
        "booking_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("flight_id", sa.Uuid(), nullable=False),
        sa.Column("seat_id", sa.Uuid(), nullable=False),
        sa.Column("class_type", flight_class_type_type, nullable=False),
        sa.Column("fare_type", fare_type_type, nullable=False),
        sa.Column("passenger_name", sa.String(255), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["seat_id"], ["flight_seats.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("price >= 0", name="ck_booking_items_price_non_negative"),
    )
    op.create_index(op.f("ix_booking_items_booking_id"), "booking_items", ["booking_id"], unique=False)
    op.create_index(op.f("ix_booking_items_flight_id"), "booking_items", ["flight_id"], unique=False)
    op.create_index(op.f("ix_booking_items_seat_id"), "booking_items", ["seat_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.drop_index(op.f("ix_booking_items_seat_id"), table_name="booking_items")
    op.drop_index(op.f("ix_booking_items_flight_id"), table_name="booking_items")
    op.drop_index(op.f("ix_booking_items_booking_id"), table_name="booking_items")
    op.drop_table("booking_items")

    op.drop_index(op.f("ix_bookings_hold_expires_at"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_idempotency_key"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_status"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_flight_id"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_user_id"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_booking_reference"), table_name="bookings")
    op.drop_table("bookings")

    if is_postgres:
        postgresql.ENUM("PENDING", "CONFIRMED", "CANCELLED", "EXPIRED", name="booking_status").drop(bind, checkfirst=True)
