"""phase1a flight foundation

Revision ID: 0002_phase1a_flight_foundation
Revises: 0001_create_users_table
Create Date: 2026-09-05 23:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0002_phase1a_flight_foundation"
down_revision: Union[str, None] = "0001_create_users_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Create Enums if Postgres
    if is_postgres:
        postgresql.ENUM("PASSENGER", "OPS_AGENT", "SUPER_ADMIN", name="user_role").create(bind, checkfirst=True)
        postgresql.ENUM("SCHEDULED", "DELAYED", "CANCELLED", "COMPLETED", name="flight_status").create(bind, checkfirst=True)
        postgresql.ENUM("FIRST", "BUSINESS", "ECONOMY", name="flight_class_type").create(bind, checkfirst=True)
        postgresql.ENUM("AVAILABLE", "HELD", "BOOKED", "BLOCKED", name="seat_status").create(bind, checkfirst=True)
        postgresql.ENUM("BASIC", "FLEXIBLE", name="fare_type").create(bind, checkfirst=True)

    # Helpers for dialect-aware enum column types
    def enum_col(values: list[str], name: str):
        if is_postgres:
            return postgresql.ENUM(*values, name=name, create_type=False)
        return sa.Enum(*values, name=name, native_enum=False)

    user_role_type = enum_col(["PASSENGER", "OPS_AGENT", "SUPER_ADMIN"], "user_role")
    flight_status_type = enum_col(["SCHEDULED", "DELAYED", "CANCELLED", "COMPLETED"], "flight_status")
    flight_class_type_type = enum_col(["FIRST", "BUSINESS", "ECONOMY"], "flight_class_type")
    seat_status_type = enum_col(["AVAILABLE", "HELD", "BOOKED", "BLOCKED"], "seat_status")
    fare_type_type = enum_col(["BASIC", "FLEXIBLE"], "fare_type")

    # 2. Add role column to users with server_default PASSENGER
    op.add_column(
        "users",
        sa.Column(
            "role",
            user_role_type,
            nullable=False,
            server_default="PASSENGER",
        ),
    )

    # 3. Create flights table
    op.create_table(
        "flights",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("flight_number", sa.String(length=20), nullable=False),
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("destination", sa.String(length=10), nullable=False),
        sa.Column("departure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrival_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_capacity", sa.Integer(), nullable=False),
        sa.Column("status", flight_status_type, server_default="SCHEDULED", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("total_capacity > 0", name="ck_flights_total_capacity_positive"),
        sa.CheckConstraint("arrival_at > departure_at", name="ck_flights_arrival_after_departure"),
        sa.CheckConstraint("origin != destination", name="ck_flights_origin_ne_destination"),
    )
    op.create_index(op.f("ix_flights_flight_number"), "flights", ["flight_number"])
    op.create_index(op.f("ix_flights_origin"), "flights", ["origin"])
    op.create_index(op.f("ix_flights_destination"), "flights", ["destination"])
    op.create_index(op.f("ix_flights_departure_at"), "flights", ["departure_at"])
    op.create_index(op.f("ix_flights_status"), "flights", ["status"])
    op.create_index(op.f("ix_flights_created_by"), "flights", ["created_by"])

    # 4. Create flight_classes table
    op.create_table(
        "flight_classes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("flight_id", sa.Uuid(), nullable=False),
        sa.Column("class_type", flight_class_type_type, nullable=False),
        sa.Column("total_seats", sa.Integer(), nullable=False),
        sa.Column("available_seats", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("flight_id", "class_type", name="uq_flight_classes_flight_class"),
        sa.CheckConstraint("total_seats > 0", name="ck_flight_classes_total_seats_positive"),
        sa.CheckConstraint("available_seats >= 0", name="ck_flight_classes_available_seats_non_negative"),
        sa.CheckConstraint("available_seats <= total_seats", name="ck_flight_classes_available_lte_total"),
    )
    op.create_index(op.f("ix_flight_classes_flight_id"), "flight_classes", ["flight_id"])

    # 5. Create flight_seats table
    op.create_table(
        "flight_seats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("flight_id", sa.Uuid(), nullable=False),
        sa.Column("seat_number", sa.String(length=10), nullable=False),
        sa.Column("class_type", flight_class_type_type, nullable=False),
        sa.Column("status", seat_status_type, server_default="AVAILABLE", nullable=False),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("flight_id", "seat_number", name="uq_flight_seats_flight_seat"),
    )
    op.create_index(op.f("ix_flight_seats_flight_id"), "flight_seats", ["flight_id"])
    op.create_index(op.f("ix_flight_seats_status"), "flight_seats", ["status"])

    # 6. Create fare_rules table
    op.create_table(
        "fare_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("flight_id", sa.Uuid(), nullable=False),
        sa.Column("class_type", flight_class_type_type, nullable=False),
        sa.Column("fare_type", fare_type_type, nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="USD", nullable=False),
        sa.Column("changes_allowed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("seat_selection_allowed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("refundable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("credit_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cancellation_cutoff_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("flight_id", "class_type", "fare_type", name="uq_fare_rules_flight_class_fare"),
        sa.CheckConstraint("price >= 0", name="ck_fare_rules_price_non_negative"),
        sa.CheckConstraint("cancellation_cutoff_minutes >= 0", name="ck_fare_rules_cancellation_cutoff_non_negative"),
    )
    op.create_index(op.f("ix_fare_rules_flight_id"), "fare_rules", ["flight_id"])

    # 7. Create audit_logs table
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("old_values", sa.JSON(), nullable=True),
        sa.Column("new_values", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(op.f("ix_audit_logs_user_id"), "audit_logs", ["user_id"])
    op.create_index(op.f("ix_audit_logs_entity_id"), "audit_logs", ["entity_id"])
    op.create_index(op.f("ix_audit_logs_created_at"), "audit_logs", ["created_at"])

    # 8. Enable RLS on Postgres
    if is_postgres:
        op.execute("ALTER TABLE flights ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE flight_classes ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE flight_seats ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE fare_rules ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Drop tables in reverse order
    op.drop_table("audit_logs")
    op.drop_table("fare_rules")
    op.drop_table("flight_seats")
    op.drop_table("flight_classes")
    op.drop_table("flights")

    # Drop role column from users
    op.drop_column("users", "role")

    # Drop enums if Postgres
    if is_postgres:
        postgresql.ENUM(name="fare_type").drop(bind, checkfirst=True)
        postgresql.ENUM(name="seat_status").drop(bind, checkfirst=True)
        postgresql.ENUM(name="flight_class_type").drop(bind, checkfirst=True)
        postgresql.ENUM(name="flight_status").drop(bind, checkfirst=True)
        postgresql.ENUM(name="user_role").drop(bind, checkfirst=True)
