"""phase6a automation infrastructure and n8n readiness

Revision ID: 0006_phase6a_automation_infra
Revises: 0005_phase5a_waitlist_standby
Create Date: 2026-09-06 02:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic. (Must be <= 32 chars for alembic_version table)
revision: str = "0006_phase6a_automation_infra"
down_revision: Union[str, None] = "0005_phase5a_waitlist_standby"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Add origin_timezone and destination_timezone to flights
    op.add_column(
        "flights",
        sa.Column("origin_timezone", sa.String(50), nullable=False, server_default="UTC"),
    )
    op.add_column(
        "flights",
        sa.Column("destination_timezone", sa.String(50), nullable=False, server_default="UTC"),
    )

    # 2. Create Enums if Postgres
    if is_postgres:
        postgresql.ENUM(
            "CHECK_IN_REMINDER", "PRICE_DROP_ALERT", "FLIGHT_CANCELLATION", "SCHEDULE_CHANGE", "BOOKING_CONFIRMATION",
            name="notification_type",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "PENDING", "PROCESSING", "SENT", "FAILED", "CANCELLED",
            name="notification_status",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "EMAIL", "SMS", "IN_APP",
            name="notification_channel",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "MANUAL_UPDATE", "DYNAMIC_PRICING", "PROMOTION", "INITIAL_CONFIG",
            name="price_change_reason",
        ).create(bind, checkfirst=True)

    def enum_col(values: list[str], name: str):
        if is_postgres:
            return postgresql.ENUM(*values, name=name, create_type=False)
        return sa.Enum(*values, name=name, native_enum=False)

    notification_type_col = enum_col(
        ["CHECK_IN_REMINDER", "PRICE_DROP_ALERT", "FLIGHT_CANCELLATION", "SCHEDULE_CHANGE", "BOOKING_CONFIRMATION"],
        "notification_type",
    )
    notification_status_col = enum_col(
        ["PENDING", "PROCESSING", "SENT", "FAILED", "CANCELLED"],
        "notification_status",
    )
    notification_channel_col = enum_col(
        ["EMAIL", "SMS", "IN_APP"],
        "notification_channel",
    )
    price_change_reason_col = enum_col(
        ["MANUAL_UPDATE", "DYNAMIC_PRICING", "PROMOTION", "INITIAL_CONFIG"],
        "price_change_reason",
    )
    flight_class_type_col = enum_col(["FIRST", "BUSINESS", "ECONOMY"], "flight_class_type")
    fare_type_col = enum_col(["BASIC", "FLEXIBLE"], "fare_type")

    # 3. Create price_history table
    op.create_table(
        "price_history",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("fare_rule_id", sa.Uuid(as_uuid=True), sa.ForeignKey("fare_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("flight_id", sa.Uuid(as_uuid=True), sa.ForeignKey("flights.id", ondelete="CASCADE"), nullable=False),
        sa.Column("class_type", flight_class_type_col, nullable=False),
        sa.Column("fare_type", fare_type_col, nullable=False),
        sa.Column("old_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("new_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("price_delta", sa.Numeric(10, 2), nullable=False),
        sa.Column("percentage_drop", sa.Numeric(5, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("changed_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", price_change_reason_col, nullable=False, server_default="MANUAL_UPDATE"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("old_price >= 0", name="ck_price_history_old_price_non_negative"),
        sa.CheckConstraint("new_price >= 0", name="ck_price_history_new_price_non_negative"),
    )
    op.create_index("ix_price_history_fare_rule", "price_history", ["fare_rule_id", "created_at"])
    op.create_index("ix_price_history_flight_class_time", "price_history", ["flight_id", "class_type", "created_at"])

    # 4. Create notifications table
    payload_col = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("notification_type", notification_type_col, nullable=False),
        sa.Column("channel", notification_channel_col, nullable=False, server_default="EMAIL"),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("status", notification_status_col, nullable=False, server_default="PENDING"),
        sa.Column("flight_id", sa.Uuid(as_uuid=True), sa.ForeignKey("flights.id", ondelete="SET NULL"), nullable=True),
        sa.Column("booking_id", sa.Uuid(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("deduplication_key", sa.String(255), nullable=False),
        sa.Column("payload", payload_col, nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("deduplication_key", name="uq_notifications_deduplication_key"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notifications_attempt_count_non_negative"),
        sa.CheckConstraint("max_retries >= 0", name="ck_notifications_max_retries_non_negative"),
    )
    op.create_index("ix_notifications_claim_worker", "notifications", ["status", "scheduled_for", "attempt_count"])
    op.create_index("ix_notifications_flight_type", "notifications", ["flight_id", "notification_type"])
    op.create_index("ix_notifications_recipient", "notifications", ["recipient"])

    # 5. Create SQL Views (PostgreSQL)
    if is_postgres:
        # View 1: Flight Operational Metrics (Load factor, Gross Revenue, Refunds, Net Revenue)
        op.execute("""
        CREATE OR REPLACE VIEW v_flight_operational_metrics AS
        SELECT 
            f.id AS flight_id,
            f.flight_number,
            f.origin,
            f.destination,
            f.origin_timezone,
            f.destination_timezone,
            f.departure_at,
            f.arrival_at,
            f.status AS flight_status,
            f.total_capacity,
            COALESCE(SUM(CASE WHEN s.status = 'BOOKED' THEN 1 ELSE 0 END), 0) AS booked_seats,
            COALESCE(SUM(CASE WHEN s.status = 'HELD' THEN 1 ELSE 0 END), 0) AS held_seats,
            COALESCE(SUM(CASE WHEN s.status = 'AVAILABLE' THEN 1 ELSE 0 END), 0) AS available_seats,
            CASE 
                WHEN f.total_capacity > 0 THEN 
                    ROUND((COALESCE(SUM(CASE WHEN s.status = 'BOOKED' THEN 1 ELSE 0 END), 0)::numeric / f.total_capacity::numeric) * 100, 2)
                ELSE 0.00 
            END AS load_factor_percentage,
            COALESCE(rev.gross_revenue, 0.00) AS gross_revenue,
            COALESCE(ref.total_refunded, 0.00) AS total_refunded,
            (COALESCE(rev.gross_revenue, 0.00) - COALESCE(ref.total_refunded, 0.00)) AS net_revenue
        FROM flights f
        LEFT JOIN flight_seats s ON f.id = s.flight_id
        LEFT JOIN (
            SELECT 
                b.flight_id,
                SUM(bi.price) AS gross_revenue
            FROM bookings b
            JOIN booking_items bi ON b.id = bi.booking_id
            WHERE b.status = 'CONFIRMED' AND bi.status = 'CONFIRMED'
            GROUP BY b.flight_id
        ) rev ON f.id = rev.flight_id
        LEFT JOIN (
            SELECT 
                b.flight_id,
                SUM(r.amount) AS total_refunded
            FROM refunds r
            JOIN bookings b ON r.booking_id = b.id
            WHERE r.status IN ('COMPLETED', 'APPROVED', 'PENDING') AND r.refund_type = 'MONETARY'
            GROUP BY b.flight_id
        ) ref ON f.id = ref.flight_id
        GROUP BY f.id, rev.gross_revenue, ref.total_refunded;
        """)

        # View 2: Eligible Check-in Reminders with Automatic Cancellation Suppression
        op.execute("""
        CREATE OR REPLACE VIEW v_eligible_checkin_reminders AS
        SELECT 
            b.id AS booking_id,
            b.booking_reference,
            b.user_id AS passenger_id,
            u.name AS passenger_name,
            u.email AS passenger_email,
            f.id AS flight_id,
            f.flight_number,
            f.origin,
            f.destination,
            f.origin_timezone,
            f.destination_timezone,
            f.departure_at,
            f.arrival_at,
            f.status AS flight_status,
            ('checkin:' || b.id || ':' || f.id || ':' || u.id) AS deduplication_key
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        JOIN flights f ON b.flight_id = f.id
        WHERE b.status = 'CONFIRMED'
          AND f.status = 'SCHEDULED'
          AND NOT EXISTS (
              SELECT 1 FROM notifications n
              WHERE n.deduplication_key = ('checkin:' || b.id || ':' || f.id || ':' || u.id)
                AND n.status IN ('SENT', 'PROCESSING')
          );
        """)

        # 6. Enable Row Level Security (RLS) on all remaining tables
        # Resolves Supabase UNRESTRICTED badge while keeping direct backend connections fully functional
        op.execute("ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE booking_items ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE booking_changes ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE refunds ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.execute("DROP VIEW IF EXISTS v_eligible_checkin_reminders;")
        op.execute("DROP VIEW IF EXISTS v_flight_operational_metrics;")

    op.drop_table("notifications")
    op.drop_table("price_history")

    if is_postgres:
        op.execute("DROP TYPE IF EXISTS price_change_reason CASCADE;")
        op.execute("DROP TYPE IF EXISTS notification_channel CASCADE;")
        op.execute("DROP TYPE IF EXISTS notification_status CASCADE;")
        op.execute("DROP TYPE IF EXISTS notification_type CASCADE;")

    op.drop_column("flights", "destination_timezone")
    op.drop_column("flights", "origin_timezone")
