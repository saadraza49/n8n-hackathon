from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import NotificationStatus, NotificationType, SeatStatus
from app.models.flight import Flight
from app.models.flight_seat import FlightSeat
from app.models.booking import Booking, BookingItem
from app.models.notification import Notification
from app.models.price_history import PriceHistory
from app.schemas.notification import NotificationCreate, NotificationStatusUpdateRequest
from app.schemas.reporting import (
    DailyOperationalSummaryResponse,
    EligibleCheckinReminderResponse,
    FlightOperationalMetricsResponse,
)


def get_eligible_checkin_reminders(
    db: Session,
    max_hours_ahead: int = 24,
) -> List[EligibleCheckinReminderResponse]:
    """
    Retrieve passengers eligible for check-in reminder emails.
    Automatically suppresses reminders for cancelled flights.
    Uses database view or equivalent query.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_future = now_utc + timedelta(hours=max_hours_ahead)

    is_postgres = db.bind.dialect.name == "postgresql" if db.bind else False

    if is_postgres:
        sql = text("""
            SELECT 
                booking_id,
                booking_reference,
                passenger_id,
                passenger_name,
                passenger_email,
                flight_id,
                flight_number,
                origin,
                destination,
                origin_timezone,
                destination_timezone,
                departure_at,
                arrival_at,
                flight_status,
                deduplication_key
            FROM v_eligible_checkin_reminders
            WHERE departure_at <= :cutoff AND departure_at > :now
            ORDER BY departure_at ASC
        """)
        rows = db.execute(sql, {"cutoff": cutoff_future, "now": now_utc}).fetchall()
        results = []
        for r in rows:
            results.append(
                EligibleCheckinReminderResponse(
                    booking_id=r[0],
                    booking_reference=r[1],
                    passenger_id=r[2],
                    passenger_name=r[3],
                    passenger_email=r[4],
                    flight_id=r[5],
                    flight_number=r[6],
                    origin=r[7],
                    destination=r[8],
                    origin_timezone=r[9] or "UTC",
                    destination_timezone=r[10] or "UTC",
                    departure_at=r[11],
                    arrival_at=r[12],
                    flight_status=r[13],
                    deduplication_key=r[14],
                )
            )
        return results

    # Universal query for SQLite in-memory tests
    from app.models.user import User
    from app.models.enums import BookingStatus, FlightStatus

    sent_keys_subquery = (
        db.query(Notification.deduplication_key)
        .filter(Notification.status.in_([NotificationStatus.SENT, NotificationStatus.PROCESSING]))
        .subquery()
    )

    query = (
        db.query(Booking, Flight, User)
        .join(Flight, Booking.flight_id == Flight.id)
        .join(User, Booking.user_id == User.id)
        .filter(
            Booking.status == BookingStatus.CONFIRMED,
            Flight.status == FlightStatus.SCHEDULED,
            Flight.departure_at <= cutoff_future,
            Flight.departure_at > now_utc,
        )
    )

    results = []
    for b, f, u in query.all():
        dedup_key = f"checkin:{b.id}:{f.id}:{u.id}"
        # Check if already processed
        exists = db.query(Notification).filter(
            Notification.deduplication_key == dedup_key,
            Notification.status.in_([NotificationStatus.SENT, NotificationStatus.PROCESSING]),
        ).first()
        if not exists:
            results.append(
                EligibleCheckinReminderResponse(
                    booking_id=b.id,
                    booking_reference=b.booking_reference,
                    passenger_id=u.id,
                    passenger_name=u.name,
                    passenger_email=u.email,
                    flight_id=f.id,
                    flight_number=f.flight_number,
                    origin=f.origin,
                    destination=f.destination,
                    origin_timezone=getattr(f, "origin_timezone", "UTC") or "UTC",
                    destination_timezone=getattr(f, "destination_timezone", "UTC") or "UTC",
                    departure_at=f.departure_at,
                    arrival_at=f.arrival_at,
                    flight_status=f.status.value,
                    deduplication_key=dedup_key,
                )
            )
    return results


def get_flight_operational_metrics(
    db: Session,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> DailyOperationalSummaryResponse:
    """
    Calculate operational metrics (load factor %, revenue, refunds, net revenue)
    across flights within an optional departure date range.
    """
    is_postgres = db.bind.dialect.name == "postgresql" if db.bind else False

    flight_query = db.query(Flight)
    if start_date:
        start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=timezone.utc)
        flight_query = flight_query.filter(Flight.departure_at >= start_dt)
    if end_date:
        end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)
        flight_query = flight_query.filter(Flight.departure_at <= end_dt)

    flights = flight_query.order_by(Flight.departure_at.asc()).all()

    flight_responses: List[FlightOperationalMetricsResponse] = []
    total_booked_all = 0
    total_gross_all = Decimal("0.00")
    total_refunds_all = Decimal("0.00")
    total_capacity_all = 0

    for f in flights:
        # Load seats count
        seats = db.query(FlightSeat).filter(FlightSeat.flight_id == f.id).all()
        booked_seats = sum(1 for s in seats if s.status == SeatStatus.BOOKED)
        held_seats = sum(1 for s in seats if s.status == SeatStatus.HELD)
        available_seats = sum(1 for s in seats if s.status == SeatStatus.AVAILABLE)

        load_factor = (
            round(Decimal(booked_seats) / Decimal(f.total_capacity) * Decimal(100), 2)
            if f.total_capacity > 0
            else Decimal("0.00")
        )

        # Gross revenue from confirmed items on confirmed bookings
        gross_items = (
            db.query(BookingItem.price)
            .join(Booking, BookingItem.booking_id == Booking.id)
            .filter(
                Booking.flight_id == f.id,
                Booking.status == "CONFIRMED",
                BookingItem.status == "CONFIRMED",
            )
            .all()
        )
        gross_revenue = sum((it[0] for it in gross_items), Decimal("0.00"))

        # Refunds issued for this flight
        from app.models.booking import Refund
        from app.models.enums import RefundStatus, RefundType
        refund_items = (
            db.query(Refund.amount)
            .join(Booking, Refund.booking_id == Booking.id)
            .filter(
                Booking.flight_id == f.id,
                Refund.status.in_([RefundStatus.PENDING, RefundStatus.APPROVED, RefundStatus.COMPLETED]),
                Refund.refund_type == RefundType.MONETARY,
            )
            .all()
        )
        total_refunded = sum((r[0] for r in refund_items), Decimal("0.00"))
        net_revenue = gross_revenue - total_refunded

        total_booked_all += booked_seats
        total_gross_all += gross_revenue
        total_refunds_all += total_refunded
        total_capacity_all += f.total_capacity

        flight_responses.append(
            FlightOperationalMetricsResponse(
                flight_id=f.id,
                flight_number=f.flight_number,
                origin=f.origin,
                destination=f.destination,
                origin_timezone=getattr(f, "origin_timezone", "UTC") or "UTC",
                destination_timezone=getattr(f, "destination_timezone", "UTC") or "UTC",
                departure_at=f.departure_at,
                arrival_at=f.arrival_at,
                flight_status=f.status.value,
                total_capacity=f.total_capacity,
                booked_seats=booked_seats,
                held_seats=held_seats,
                available_seats=available_seats,
                load_factor_percentage=load_factor,
                gross_revenue=gross_revenue,
                total_refunded=total_refunded,
                net_revenue=net_revenue,
            )
        )

    avg_load_factor = (
        round(Decimal(total_booked_all) / Decimal(total_capacity_all) * Decimal(100), 2)
        if total_capacity_all > 0
        else Decimal("0.00")
    )
    total_net_all = total_gross_all - total_refunds_all

    return DailyOperationalSummaryResponse(
        total_flights=len(flights),
        total_booked_passengers=total_booked_all,
        average_load_factor=avg_load_factor,
        total_gross_revenue=total_gross_all,
        total_refunds=total_refunds_all,
        total_net_revenue=total_net_all,
        flights=flight_responses,
    )


def claim_pending_notifications(
    db: Session,
    batch_size: int = 10,
) -> List[Notification]:
    """
    Concurrent worker claim mechanism demonstrating PostgreSQL row-level locking:
    SELECT ... FROM notifications WHERE status = 'PENDING' FOR UPDATE SKIP LOCKED.
    Atomically transitions claimed notifications to PROCESSING.
    """
    now_utc = datetime.now(timezone.utc)

    # In PostgreSQL, with_for_update(skip_locked=True) locks rows; in SQLite it selects
    claimed = (
        db.query(Notification)
        .filter(
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED]),
            Notification.scheduled_for <= now_utc,
            Notification.attempt_count < Notification.max_retries,
            (Notification.next_retry_at.is_(None) | (Notification.next_retry_at <= now_utc)),
        )
        .order_by(Notification.scheduled_for.asc())
        .with_for_update(skip_locked=True)
        .limit(batch_size)
        .all()
    )

    if not claimed:
        return []

    for item in claimed:
        item.status = NotificationStatus.PROCESSING
        item.processing_started_at = now_utc
        item.attempt_count += 1

    db.commit()
    for item in claimed:
        db.refresh(item)

    return claimed


def update_notification_status(
    db: Session,
    notification_id: UUID,
    payload: NotificationStatusUpdateRequest,
) -> Notification:
    """Update delivery result of a notification (SENT, FAILED, CANCELLED)."""
    now_utc = datetime.now(timezone.utc)
    notification = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification with id '{notification_id}' not found.",
        )

    notification.status = payload.status
    if payload.status == NotificationStatus.SENT:
        notification.sent_at = now_utc
        notification.last_error = None
    elif payload.status == NotificationStatus.FAILED:
        notification.last_error = payload.error_message
        if payload.next_retry_minutes and notification.attempt_count < notification.max_retries:
            notification.next_retry_at = now_utc + timedelta(minutes=payload.next_retry_minutes)
    elif payload.status == NotificationStatus.CANCELLED:
        notification.last_error = payload.error_message

    db.commit()
    db.refresh(notification)
    return notification


def create_notification(
    db: Session,
    payload: NotificationCreate,
) -> Notification:
    """Create a notification with database-enforced deduplication key uniqueness."""
    now_utc = datetime.now(timezone.utc)
    notification = Notification(
        id=uuid4(),
        user_id=payload.user_id,
        notification_type=payload.notification_type,
        channel=payload.channel,
        recipient=payload.recipient,
        status=NotificationStatus.PENDING,
        flight_id=payload.flight_id,
        booking_id=payload.booking_id,
        deduplication_key=payload.deduplication_key,
        payload=payload.payload,
        scheduled_for=payload.scheduled_for or now_utc,
    )
    db.add(notification)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Notification with deduplication key '{payload.deduplication_key}' already exists.",
        )
    db.refresh(notification)
    return notification


def get_price_history_for_flight(
    db: Session,
    flight_id: UUID,
) -> List[PriceHistory]:
    """Retrieve historical price movements for a flight."""
    return (
        db.query(PriceHistory)
        .filter(PriceHistory.flight_id == flight_id)
        .order_by(PriceHistory.created_at.desc())
        .all()
    )
