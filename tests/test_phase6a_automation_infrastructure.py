from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Tuple
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.models.booking import Booking, BookingItem
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FareType,
    FlightClassType,
    FlightStatus,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
    PriceChangeReason,
    SeatStatus,
    UserRole,
)
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.notification import Notification
from app.models.price_history import PriceHistory
from app.models.user import User
from app.services.automation_service import (
    claim_pending_notifications,
    create_notification,
    get_eligible_checkin_reminders,
    get_flight_operational_metrics,
    update_notification_status,
)
from app.schemas.notification import NotificationCreate, NotificationStatusUpdateRequest


# ============================================================================
# TEST FIXTURES & HELPERS
# ============================================================================

def create_user_with_role(db_session: Session, role: UserRole, email_prefix: str = "user") -> Tuple[User, str]:
    """Create user and return User object and JWT access token."""
    user = User(
        name=f"Test {role.value}",
        email=f"{email_prefix}_{uuid4().hex[:6]}@example.com",
        password_hash=hash_password("test_password123"),
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(data={"sub": str(user.id), "email": user.email})
    return user, token


def create_test_flight_with_timezones(
    client,
    ops_token: str,
    flight_number: str = "PK901",
    hours_ahead: int = 18,
    origin_tz: str = "Asia/Karachi",
    dest_tz: str = "Asia/Karachi",
) -> Tuple[str, dict]:
    """Create a flight with explicit timezones for check-in and automation tests."""
    now_utc = datetime.now(timezone.utc)
    dep_at = now_utc + timedelta(hours=hours_ahead)
    arr_at = dep_at + timedelta(hours=2)

    flight_res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "flight_number": flight_number,
            "origin": "ISB",
            "destination": "KHI",
            "departure_at": dep_at.isoformat(),
            "arrival_at": arr_at.isoformat(),
            "total_capacity": 4,
            "first_class_seats": 1,
            "business_class_seats": 1,
            "economy_class_seats": 2,
        },
    )
    assert flight_res.status_code == 201, flight_res.text
    flight_id = flight_res.json()["id"]

    # Fares
    client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "FLEXIBLE",
            "price": "150.00",
            "currency": "USD",
            "seat_selection_allowed": True,
            "changes_allowed": True,
            "refundable": True,
            "cancellation_cutoff_minutes": 60,
        },
    )
    return flight_id, flight_res.json()


# ============================================================================
# 1. NOTIFICATION LIFECYCLE & TRANSITIONS
# ============================================================================

def test_notification_valid_creation_and_state_transitions(db_session):
    """
    Test creating a notification record and transitioning through valid lifecycle
    PENDING -> PROCESSING -> SENT.
    """
    user, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_notif_1")
    dedup_key = f"test:remind:{user.id}:{uuid4().hex}"

    notif = create_notification(
        db=db_session,
        payload=NotificationCreate(
            user_id=user.id,
            notification_type=NotificationType.CHECK_IN_REMINDER,
            channel=NotificationChannel.EMAIL,
            recipient=user.email,
            deduplication_key=dedup_key,
            payload={"passenger_name": user.name},
        ),
    )
    assert notif.id is not None
    assert notif.status == NotificationStatus.PENDING
    assert notif.attempt_count == 0

    # Claim notification (emulating n8n worker claim)
    claimed = claim_pending_notifications(db=db_session, batch_size=10)
    assert len(claimed) >= 1
    target = next((n for n in claimed if n.id == notif.id), None)
    assert target is not None
    assert target.status == NotificationStatus.PROCESSING
    assert target.processing_started_at is not None
    assert target.attempt_count == 1

    # Mark as SENT (emulating successful Gmail delivery in n8n)
    updated = update_notification_status(
        db=db_session,
        notification_id=notif.id,
        payload=NotificationStatusUpdateRequest(status=NotificationStatus.SENT),
    )
    assert updated.status == NotificationStatus.SENT
    assert updated.sent_at is not None
    assert updated.last_error is None


def test_notification_failure_and_retry_scheduling(db_session):
    """
    Test notification failure records error message and schedules next retry timestamp.
    """
    user, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_notif_fail")
    dedup_key = f"test:fail:{user.id}:{uuid4().hex}"

    notif = create_notification(
        db=db_session,
        payload=NotificationCreate(
            user_id=user.id,
            notification_type=NotificationType.PRICE_DROP_ALERT,
            channel=NotificationChannel.EMAIL,
            recipient=user.email,
            deduplication_key=dedup_key,
        ),
    )

    # Transition to FAILED with error
    updated = update_notification_status(
        db=db_session,
        notification_id=notif.id,
        payload=NotificationStatusUpdateRequest(
            status=NotificationStatus.FAILED,
            error_message="SMTP 550: Mailbox unavailable",
            next_retry_minutes=15,
        ),
    )
    assert updated.status == NotificationStatus.FAILED
    assert updated.last_error == "SMTP 550: Mailbox unavailable"
    assert updated.next_retry_at is not None


# ============================================================================
# 2. DETERMINISTIC DEDUPLICATION ENFORCEMENT
# ============================================================================

def test_duplicate_deduplication_key_rejected_at_db_level(db_session):
    """
    Test that database strictly enforces UNIQUE (deduplication_key).
    Attempting to create a duplicate key is rejected with 409 Conflict.
    """
    user, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_dedup")
    fixed_key = f"checkin:booking_fixed:{user.id}"

    create_notification(
        db=db_session,
        payload=NotificationCreate(
            user_id=user.id,
            notification_type=NotificationType.CHECK_IN_REMINDER,
            channel=NotificationChannel.EMAIL,
            recipient=user.email,
            deduplication_key=fixed_key,
        ),
    )

    # Second attempt with exact same key fails
    with pytest.raises(Exception) as exc_info:
        create_notification(
            db=db_session,
            payload=NotificationCreate(
                user_id=user.id,
                notification_type=NotificationType.CHECK_IN_REMINDER,
                channel=NotificationChannel.EMAIL,
                recipient=user.email,
                deduplication_key=fixed_key,
            ),
        )
    assert "already exists" in str(exc_info.value) or "409" in str(exc_info.value)


# ============================================================================
# 3. CHECK-IN REMINDER ELIGIBILITY & CANCELLED FLIGHT SUPPRESSION
# ============================================================================

def test_checkin_reminder_eligibility_and_detection(client, db_session):
    """
    Verify that passengers with confirmed bookings on scheduled flights departing
    within the reminder window (24h) are discovered by get_eligible_checkin_reminders.
    """
    _, ops_tok = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_remind")
    flight_id, _ = create_test_flight_with_timezones(client, ops_tok, flight_number="PK910", hours_ahead=12)

    # Book and confirm
    hold_res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_tok}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}]},
    )
    b_id = hold_res.json()["id"]
    client.post(f"/bookings/{b_id}/confirm", headers={"Authorization": f"Bearer {p_tok}"})

    # Query eligible reminders
    eligible = get_eligible_checkin_reminders(db=db_session, max_hours_ahead=24)
    matching = [e for e in eligible if str(e.booking_id) == b_id]
    assert len(matching) == 1
    assert matching[0].flight_number == "PK910"
    assert matching[0].flight_status == "SCHEDULED"
    assert matching[0].deduplication_key.startswith("checkin:")


def test_cancelled_flight_suppresses_checkin_reminders(client, db_session):
    """
    CRITICAL REQUIREMENT:
    When a flight is cancelled, get_eligible_checkin_reminders immediately suppresses
    all reminder emails for all passengers on that flight.
    """
    _, ops_tok = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_suppress")
    flight_id, _ = create_test_flight_with_timezones(client, ops_tok, flight_number="PK911", hours_ahead=10)

    hold_res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_tok}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}]},
    )
    b_id = hold_res.json()["id"]
    client.post(f"/bookings/{b_id}/confirm", headers={"Authorization": f"Bearer {p_tok}"})

    # Verify initially eligible
    eligible_before = get_eligible_checkin_reminders(db=db_session, max_hours_ahead=24)
    assert any(str(e.booking_id) == b_id for e in eligible_before)

    # Cancel the flight via operational API
    cancel_res = client.post(f"/flights/{flight_id}/cancel", headers={"Authorization": f"Bearer {ops_tok}"})
    assert cancel_res.status_code == 200

    # Verify immediately suppressed
    db_session.rollback()
    db_session.expire_all()
    eligible_after = get_eligible_checkin_reminders(db=db_session, max_hours_ahead=24)
    assert not any(str(e.booking_id) == b_id for e in eligible_after)


def test_already_sent_reminder_is_not_re_eligible(client, db_session):
    """
    Once a reminder is marked SENT or PROCESSING, it is automatically excluded from
    the eligible reminder query.
    """
    _, ops_tok = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, p_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_sent_check")
    flight_id, _ = create_test_flight_with_timezones(client, ops_tok, flight_number="PK912", hours_ahead=15)

    hold_res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_tok}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}]},
    )
    b_id = hold_res.json()["id"]
    client.post(f"/bookings/{b_id}/confirm", headers={"Authorization": f"Bearer {p_tok}"})

    eligible = get_eligible_checkin_reminders(db=db_session, max_hours_ahead=24)
    matching = next(e for e in eligible if str(e.booking_id) == b_id)

    # Record notification as SENT
    notif = create_notification(
        db=db_session,
        payload=NotificationCreate(
            user_id=passenger.id,
            notification_type=NotificationType.CHECK_IN_REMINDER,
            channel=NotificationChannel.EMAIL,
            recipient=passenger.email,
            deduplication_key=matching.deduplication_key,
        ),
    )
    update_notification_status(
        db=db_session,
        notification_id=notif.id,
        payload=NotificationStatusUpdateRequest(status=NotificationStatus.SENT),
    )

    # Verify no longer eligible
    db_session.rollback()
    db_session.expire_all()
    eligible_after = get_eligible_checkin_reminders(db=db_session, max_hours_ahead=24)
    assert not any(str(e.booking_id) == b_id for e in eligible_after)


# ============================================================================
# 4. PRICE HISTORY & PRICE DROP DEDUPLICATION
# ============================================================================

def test_price_history_recording_on_fare_update(client, db_session):
    """
    When an operator updates a fare rule price, PriceHistory is persisted automatically
    with exact numeric precision, price delta, and percentage drop.
    """
    ops_user, ops_tok = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_ph")
    flight_id, _ = create_test_flight_with_timezones(client, ops_tok, flight_number="PK915")

    # Get fare rule ID
    fares = client.get(f"/flights/{flight_id}/fares", headers={"Authorization": f"Bearer {ops_tok}"}).json()
    fare_id = fares[0]["id"]
    assert Decimal(fares[0]["price"]) == Decimal("150.00")

    # Update price from 150.00 -> 120.00 (20% drop)
    update_res = client.patch(
        f"/flights/{flight_id}/fares/{fare_id}",
        headers={"Authorization": f"Bearer {ops_tok}"},
        json={"price": "120.00"},
    )
    assert update_res.status_code == 200

    # Verify PriceHistory record exists in database
    db_session.rollback()
    db_session.expire_all()
    history = db_session.query(PriceHistory).filter(PriceHistory.fare_rule_id == UUID(fare_id)).first()
    assert history is not None
    assert history.old_price == Decimal("150.00")
    assert history.new_price == Decimal("120.00")
    assert history.price_delta == Decimal("-30.00")
    assert history.percentage_drop == Decimal("20.00")
    assert history.reason == PriceChangeReason.MANUAL_UPDATE


def test_price_drop_deduplication_key_cooldown(db_session):
    """
    Demonstrate that price drop alert deduplication keys suppress multiple micro-fluctuations.
    """
    user, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_pdrop")
    flight_id = uuid4()
    today_str = date.today().isoformat()

    # Rule: at most 1 alert per flight+class per day
    daily_dedup_key = f"pricedrop:{flight_id}:ECONOMY:{today_str}"

    n1 = create_notification(
        db=db_session,
        payload=NotificationCreate(
            user_id=user.id,
            notification_type=NotificationType.PRICE_DROP_ALERT,
            channel=NotificationChannel.EMAIL,
            recipient=user.email,
            deduplication_key=daily_dedup_key,
            payload={"old_price": "200.00", "new_price": "180.00"},
        ),
    )
    assert n1.id is not None

    # Subsequent micro-drop on same day with same key is rejected by database constraint
    with pytest.raises(Exception):
        create_notification(
            db=db_session,
            payload=NotificationCreate(
                user_id=user.id,
                notification_type=NotificationType.PRICE_DROP_ALERT,
                channel=NotificationChannel.EMAIL,
                recipient=user.email,
                deduplication_key=daily_dedup_key,
                payload={"old_price": "180.00", "new_price": "178.00"},
            ),
        )


# ============================================================================
# 5. DAILY / WEEKLY OPERATIONAL REPORTING AGGREGATION
# ============================================================================

def test_daily_operational_reporting_aggregation(client, db_session):
    """
    Verify report calculation across flights:
    - Load factor % = booked_seats / total_capacity * 100
    - Gross revenue = sum of confirmed booking items
    - Total refunds = sum of completed monetary refunds
    - Net revenue = Gross revenue - Total refunds
    """
    _, ops_tok = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_rep")
    _, p1_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_rep1")
    _, p2_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_rep2")

    flight_id, _ = create_test_flight_with_timezones(client, ops_tok, flight_number="PK920", hours_ahead=20)
    # Total capacity is 4 (2 economy, 1 business, 1 first)

    # Booking 1: Passenger 1 books economy ($150.00) & confirms
    h1 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p1_tok}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}]},
    ).json()["id"]
    client.post(f"/bookings/{h1}/confirm", headers={"Authorization": f"Bearer {p1_tok}"})

    # Booking 2: Passenger 2 books economy ($150.00) & confirms
    h2 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p2_tok}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}]},
    ).json()["id"]
    client.post(f"/bookings/{h2}/confirm", headers={"Authorization": f"Bearer {p2_tok}"})

    # Passenger 2 cancels booking -> gets full $150.00 monetary refund
    client.post(f"/bookings/{h2}/cancel", headers={"Authorization": f"Bearer {p2_tok}"})

    db_session.rollback()
    db_session.expire_all()

    # Query metrics
    report = get_flight_operational_metrics(db=db_session)
    matching_flight = next(f for f in report.flights if str(f.flight_id) == flight_id)

    # 1 booked seat remaining out of 4 capacity = 25.00% load factor
    assert matching_flight.booked_seats == 1
    assert matching_flight.total_capacity == 4
    assert matching_flight.load_factor_percentage == Decimal("25.00")

    # Gross revenue from Booking 1 = 150.00
    assert matching_flight.gross_revenue == Decimal("150.00")
    # Total refunded from Booking 2 = 150.00
    assert matching_flight.total_refunded == Decimal("150.00")
    # Net revenue = 150.00 - 150.00 = 0.00
    assert matching_flight.net_revenue == Decimal("0.00")


# ============================================================================
# 6. RBAC ON AUTOMATION ENDPOINTS
# ============================================================================

def test_automation_endpoints_rbac(client, db_session):
    """Verify that only Ops Agents and Admins can access operational report and claim endpoints."""
    _, p_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_rbac")
    _, ops_tok = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_rbac")

    # Passenger is forbidden
    res_p = client.get("/automation/reports/operational", headers={"Authorization": f"Bearer {p_tok}"})
    assert res_p.status_code == 403

    # Ops Agent is allowed
    res_ops = client.get("/automation/reports/operational", headers={"Authorization": f"Bearer {ops_tok}"})
    assert res_ops.status_code == 200
