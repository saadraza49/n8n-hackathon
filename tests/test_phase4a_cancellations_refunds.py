from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Tuple
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingChange, BookingItem, Refund
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FareType,
    FlightClassType,
    FlightStatus,
    RefundReason,
    RefundStatus,
    RefundType,
    SeatStatus,
    UserRole,
)
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User


# ==========================================
# TEST FIXTURES & HELPERS
# ==========================================

def create_user_with_role(db_session: Session, role: UserRole, email_prefix: str = "user") -> Tuple[User, str]:
    """Helper to create a user with a specific role and generate a JWT Bearer token."""
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


def create_test_flight_with_custom_fares(
    client,
    ops_token: str,
    refundable: bool = True,
    credit_only: bool = False,
    changes_allowed: bool = True,
    cutoff_minutes: int = 120,
    dep_hours_from_now: int = 48,
    flight_number: str = "PK800",
) -> Tuple[str, Decimal]:
    """Helper to create a flight and configure a custom fare rule for testing."""
    now_utc = datetime.now(timezone.utc)
    dep_at = now_utc + timedelta(hours=dep_hours_from_now)
    arr_at = dep_at + timedelta(hours=6)

    res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "flight_number": flight_number,
            "origin": "ISB",
            "destination": "LHR",
            "departure_at": dep_at.isoformat(),
            "arrival_at": arr_at.isoformat(),
            "total_capacity": 40,
            "first_class_seats": 10,
            "business_class_seats": 10,
            "economy_class_seats": 20,
        },
    )
    assert res.status_code == 201
    flight_id = res.json()["id"]

    price = Decimal("200.00")
    fare_res = client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "FLEXIBLE",
            "price": str(price),
            "currency": "USD",
            "changes_allowed": changes_allowed,
            "seat_selection_allowed": True,
            "refundable": refundable,
            "credit_only": credit_only,
            "cancellation_cutoff_minutes": cutoff_minutes,
        },
    )
    assert fare_res.status_code == 201
    return flight_id, price


def create_and_confirm_booking(client, passenger_token: str, flight_id: str, count: int = 1) -> dict:
    """Helper to hold and confirm a booking."""
    items = [
        {"class_type": "ECONOMY", "fare_type": "FLEXIBLE", "passenger_name": f"Passenger {i+1}"}
        for i in range(count)
    ]
    hold_res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {passenger_token}"},
        json={"flight_id": flight_id, "items": items},
    )
    assert hold_res.status_code == 201
    booking_id = hold_res.json()["id"]

    confirm_res = client.post(
        f"/bookings/{booking_id}/confirm",
        headers={"Authorization": f"Bearer {passenger_token}"},
    )
    assert confirm_res.status_code == 200
    return confirm_res.json()


# ==========================================
# 1. VOLUNTARY CANCELLATION & REFUND POLICY TESTS
# ==========================================

def test_full_cancellation_refundable_fare(client, db_session):
    """Verify cancelling a refundable booking issues a MONETARY refund and restores seat/inventory."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p1")
    flight_id, price = create_test_flight_with_custom_fares(client, ops_token, refundable=True, credit_only=False)

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]
    seat_id = booking["items"][0]["seat_id"]

    # Check inventory before cancellation
    db_session.rollback()
    cls_before = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_before.available_seats == 19

    # Cancel booking
    cancel_res = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p_token}"},
        json={"reason": "CUSTOMER_CANCELLATION"},
    )
    assert cancel_res.status_code == 200
    data = cancel_res.json()
    assert data["status"] == "CANCELLED"
    assert data["cancelled_items_count"] == 1
    assert data["remaining_items_count"] == 0
    assert Decimal(str(data["total_refund_amount"])) == price
    assert len(data["refunds"]) == 1
    assert data["refunds"][0]["refund_type"] == "MONETARY"
    assert data["refunds"][0]["status"] == "PENDING"

    # Verify DB state: seat is AVAILABLE, class inventory restored to 20
    db_session.rollback()
    seat = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(seat_id)).first()
    assert seat.status == SeatStatus.AVAILABLE
    assert seat.hold_expires_at is None

    cls_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_after.available_seats == 20

    # Verify Audit Log
    audit = db_session.query(AuditLog).filter(
        AuditLog.entity_id == UUID(booking_id),
        AuditLog.action == "CANCEL_BOOKING",
    ).first()
    assert audit is not None


def test_full_cancellation_credit_only_fare(client, db_session):
    """Verify cancelling a credit-only booking issues a CREDIT refund."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p2")
    flight_id, price = create_test_flight_with_custom_fares(
        client, ops_token, refundable=False, credit_only=True, flight_number="PK801"
    )

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]

    cancel_res = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert cancel_res.status_code == 200
    data = cancel_res.json()
    assert data["refunds"][0]["refund_type"] == "CREDIT"
    assert Decimal(str(data["refunds"][0]["amount"])) == price


def test_full_cancellation_non_refundable_fare(client, db_session):
    """Verify cancelling a non-refundable booking issues a NONE refund of $0.00 while releasing seat."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p3")
    flight_id, _ = create_test_flight_with_fares_non_ref = create_test_flight_with_custom_fares(
        client, ops_token, refundable=False, credit_only=False, flight_number="PK802"
    )

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]

    cancel_res = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert cancel_res.status_code == 200
    data = cancel_res.json()
    assert data["refunds"][0]["refund_type"] == "NONE"
    assert Decimal(str(data["refunds"][0]["amount"])) == Decimal("0.00")
    assert data["refunds"][0]["status"] == "COMPLETED"

    # Invariant: Seat and class inventory must still be released
    db_session.rollback()
    cls_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_after.available_seats == 20


def test_cancellation_past_cutoff_deadline_produces_no_monetary_refund(client, db_session):
    """Verify cancelling after the cutoff deadline produces NONE refund ($0.00) even for refundable fare."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p4")
    # Departure is in 1 hour (60 minutes), but cutoff is 120 minutes (2 hours)
    # This means the cancellation deadline was 1 hour ago!
    flight_id, _ = create_test_flight_with_custom_fares(
        client, ops_token, refundable=True, cutoff_minutes=120, dep_hours_from_now=1, flight_number="PK803"
    )

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]

    cancel_res = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert cancel_res.status_code == 200
    data = cancel_res.json()
    assert data["refunds"][0]["refund_type"] == "NONE"
    assert Decimal(str(data["refunds"][0]["amount"])) == Decimal("0.00")
    assert "after deadline" in data["refunds"][0]["notes"]


# ==========================================
# 2. PARTIAL CANCELLATION TESTS
# ==========================================

def test_partial_cancellation_one_item(client, db_session):
    """Verify cancelling one item out of a multi-passenger booking leaves remaining items CONFIRMED."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p5")
    flight_id, price = create_test_flight_with_custom_fares(
        client, ops_token, refundable=True, flight_number="PK804"
    )

    # 2-passenger booking
    booking = create_and_confirm_booking(client, p_token, flight_id, count=2)
    booking_id = booking["id"]
    item_1_id = booking["items"][0]["id"]
    item_1_seat_id = booking["items"][0]["seat_id"]
    item_2_seat_id = booking["items"][1]["seat_id"]

    db_session.rollback()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 18  # 20 - 2

    # Partially cancel Item 1 only
    cancel_res = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p_token}"},
        json={"item_ids": [item_1_id]},
    )
    assert cancel_res.status_code == 200
    data = cancel_res.json()
    assert data["cancelled_items_count"] == 1
    assert data["remaining_items_count"] == 1
    assert data["status"] == "CONFIRMED"  # Overall booking remains CONFIRMED
    assert len(data["refunds"]) == 1
    assert Decimal(str(data["refunds"][0]["amount"])) == price

    # Verify DB: item 1 seat is AVAILABLE, item 2 seat is STILL BOOKED
    db_session.rollback()
    seat_1 = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(item_1_seat_id)).first()
    assert seat_1.status == SeatStatus.AVAILABLE

    seat_2 = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(item_2_seat_id)).first()
    assert seat_2.status == SeatStatus.BOOKED

    # Available seats incremented by exactly 1 (from 18 to 19)
    cls_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_after.available_seats == 19


# ==========================================
# 3. DOUBLE-CANCELLATION & IDEMPOTENCY TESTS
# ==========================================

def test_double_cancellation_rejected(client, db_session):
    """Verify attempting to cancel an already cancelled booking is rejected with 400 Bad Request."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p6")
    flight_id, _ = create_test_flight_with_custom_fares(client, ops_token, flight_number="PK805")

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]

    # First cancel -> 200 OK
    res1 = client.post(f"/bookings/{booking_id}/cancel", headers={"Authorization": f"Bearer {p_token}"})
    assert res1.status_code == 200

    # Second cancel -> 400 Bad Request
    res2 = client.post(f"/bookings/{booking_id}/cancel", headers={"Authorization": f"Bearer {p_token}"})
    assert res2.status_code == 400
    assert "already cancelled" in res2.json()["detail"]


def test_cancellation_idempotency_returns_previous_result(client, db_session):
    """Verify repeating cancellation with the same idempotency key returns the previous result without duplicate refunds."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p7")
    flight_id, _ = create_test_flight_with_custom_fares(client, ops_token, flight_number="PK806")

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]
    idem_key = "cancel-idem-999"

    # First call
    res1 = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p_token}", "Idempotency-Key": idem_key},
    )
    assert res1.status_code == 200
    refund_id_1 = res1.json()["refunds"][0]["id"]

    # Replay call with same Idempotency-Key
    res2 = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p_token}", "Idempotency-Key": idem_key},
    )
    assert res2.status_code == 200
    refund_id_2 = res2.json()["refunds"][0]["id"]
    assert refund_id_1 == refund_id_2
    assert "Idempotent replay" in res2.json()["message"]

    # Invariant: Only 1 refund row exists in DB
    db_session.rollback()
    refund_count = db_session.query(Refund).filter(Refund.booking_id == UUID(booking_id)).count()
    assert refund_count == 1


def test_cancel_non_owner_forbidden(client, db_session):
    """Verify passenger 2 cannot cancel passenger 1's booking."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p8_1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p8_2")
    flight_id, _ = create_test_flight_with_custom_fares(client, ops_token, flight_number="PK807")

    booking = create_and_confirm_booking(client, p1_token, flight_id, count=1)
    booking_id = booking["id"]

    res = client.post(
        f"/bookings/{booking_id}/cancel",
        headers={"Authorization": f"Bearer {p2_token}"},
    )
    assert res.status_code == 403


def test_list_booking_refunds_and_rbac(client, db_session):
    """Verify listing booking refunds is accessible to the owner and ops/admin, but forbidden to others."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p9_1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p9_2")
    flight_id, _ = create_test_flight_with_custom_fares(client, ops_token, flight_number="PK808")

    booking = create_and_confirm_booking(client, p1_token, flight_id, count=1)
    booking_id = booking["id"]
    client.post(f"/bookings/{booking_id}/cancel", headers={"Authorization": f"Bearer {p1_token}"})

    # Owner access -> 200 OK
    res_owner = client.get(f"/bookings/{booking_id}/refunds", headers={"Authorization": f"Bearer {p1_token}"})
    assert res_owner.status_code == 200
    assert len(res_owner.json()) == 1

    # Ops access -> 200 OK
    res_ops = client.get(f"/bookings/{booking_id}/refunds", headers={"Authorization": f"Bearer {ops_token}"})
    assert res_ops.status_code == 200

    # Unauthorized passenger -> 403 Forbidden
    res_other = client.get(f"/bookings/{booking_id}/refunds", headers={"Authorization": f"Bearer {p2_token}"})
    assert res_other.status_code == 403


# ==========================================
# 4. BOOKING CHANGE / REBOOKING TESTS
# ==========================================

def test_booking_change_success(client, db_session):
    """Verify booking change swaps physical seats atomically, updates inventory, and records change history."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p10")

    # Flight 1 ($200.00)
    f1_id, _ = create_test_flight_with_custom_fares(
        client, ops_token, changes_allowed=True, flight_number="PK901"
    )
    # Flight 2 ($275.00)
    f2_id, _ = create_test_flight_with_custom_fares(
        client, ops_token, changes_allowed=True, flight_number="PK902"
    )
    # Update Flight 2 fare price to $275.00
    res_f2_fares = client.get(f"/flights/{f2_id}/fares", headers={"Authorization": f"Bearer {ops_token}"})
    f2_fare_id = res_f2_fares.json()[0]["id"]
    client.patch(
        f"/flights/{f2_id}/fares/{f2_fare_id}",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"price": "275.00"},
    )

    booking = create_and_confirm_booking(client, p_token, f1_id, count=1)
    booking_id = booking["id"]
    item_id = booking["items"][0]["id"]
    old_seat_id = booking["items"][0]["seat_id"]

    # Rebook item from Flight 1 to Flight 2
    change_res = client.post(
        f"/bookings/{booking_id}/change",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "item_id": item_id,
            "new_flight_id": f2_id,
            "new_class_type": "ECONOMY",
            "new_fare_type": "FLEXIBLE",
        },
    )
    assert change_res.status_code == 200
    data = change_res.json()
    assert data["old_flight_id"] == f1_id
    assert data["new_flight_id"] == f2_id
    assert Decimal(str(data["price_difference"])) == Decimal("75.00")  # 275 - 200

    # Invariants in DB:
    # 1. Old seat on Flight 1 is AVAILABLE
    db_session.rollback()
    s1 = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(old_seat_id)).first()
    assert s1.status == SeatStatus.AVAILABLE

    # 2. New seat on Flight 2 is BOOKED
    new_seat_id = data["new_seat_id"]
    s2 = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(new_seat_id)).first()
    assert s2.status == SeatStatus.BOOKED

    # 3. Flight 1 available_seats restored (+1), Flight 2 available_seats decremented (-1)
    c1 = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(f1_id), FlightClass.class_type == FlightClassType.ECONOMY
    ).first()
    c2 = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(f2_id), FlightClass.class_type == FlightClassType.ECONOMY
    ).first()
    assert c1.available_seats == 20
    assert c2.available_seats == 19

    # 4. BookingChange history row exists
    history_row = db_session.query(BookingChange).filter(BookingChange.id == UUID(data["id"])).first()
    assert history_row is not None
    assert history_row.old_price == Decimal("200.00")
    assert history_row.new_price == Decimal("275.00")


def test_booking_change_rejected_when_not_allowed(client, db_session):
    """Verify booking change fails when fare rule specifies changes_allowed=False."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p11")

    # Flight 1 with changes_allowed=False
    f1_id, _ = create_test_flight_with_custom_fares(
        client, ops_token, changes_allowed=False, flight_number="PK903"
    )
    f2_id, _ = create_test_flight_with_custom_fares(
        client, ops_token, changes_allowed=True, flight_number="PK904"
    )

    booking = create_and_confirm_booking(client, p_token, f1_id, count=1)
    booking_id = booking["id"]
    item_id = booking["items"][0]["id"]

    res = client.post(
        f"/bookings/{booking_id}/change",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "item_id": item_id,
            "new_flight_id": f2_id,
            "new_class_type": "ECONOMY",
            "new_fare_type": "FLEXIBLE",
        },
    )
    assert res.status_code == 400
    assert "Changes are not permitted" in res.json()["detail"]


# ==========================================
# 5. OPERATIONAL SCHEDULE & CANCELLATION IMPACT
# ==========================================

def test_flight_cancellation_impact_and_affected_bookings(client, db_session):
    """Verify ops flight cancellation preserves bookings, flags affected count, and allows operational inspection."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p12")
    flight_id, _ = create_test_flight_with_custom_fares(client, ops_token, flight_number="PK905")

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]

    # Ops cancels the flight
    cancel_res = client.post(f"/flights/{flight_id}/cancel", headers={"Authorization": f"Bearer {ops_token}"})
    assert cancel_res.status_code == 200

    # Operational query for affected bookings
    affected_res = client.get(
        f"/flights/{flight_id}/affected-bookings",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert affected_res.status_code == 200
    affected_data = affected_res.json()
    assert len(affected_data) == 1
    assert affected_data[0]["id"] == booking_id

    # Invariant: booking still exists in DB as CONFIRMED (not silently deleted or modified)
    db_session.rollback()
    booking_in_db = db_session.query(Booking).filter(Booking.id == UUID(booking_id)).first()
    assert booking_in_db.status == BookingStatus.CONFIRMED

    # Audit log records affected count
    audit = db_session.query(AuditLog).filter(
        AuditLog.entity_id == UUID(flight_id),
        AuditLog.action == "CANCEL_FLIGHT",
    ).first()
    assert audit.new_values["affected_bookings_count"] == 1


# ==========================================
# 6. CONCURRENCY & TRANSACTION ROLLBACK TESTS
# ==========================================

def test_concurrent_cancellation_race_condition(client, db_session):
    """
    Verify concurrency safety: when two concurrent threads simultaneously attempt to cancel
    the EXACT same booking, exactly ONE successfully cancels it, and inventory is restored
    EXACTLY once (not twice!).
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_race_c")
    flight_id, _ = create_test_flight_with_custom_fares(client, ops_token, flight_number="PK906")

    booking = create_and_confirm_booking(client, p_token, flight_id, count=1)
    booking_id = booking["id"]

    db_session.rollback()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 19

    def attempt_cancel():
        return client.post(f"/bookings/{booking_id}/cancel", headers={"Authorization": f"Bearer {p_token}"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_cancel)
        f2 = executor.submit(attempt_cancel)
        res1 = f1.result()
        res2 = f2.result()

    status_codes = sorted([res1.status_code, res2.status_code])
    # Exactly one must succeed (200) and one must be rejected (400 Bad Request or 409 Conflict)
    assert status_codes in ([200, 400], [200, 409])

    # Invariant: inventory must be restored exactly by 1 (19 -> 20, never 21!)
    db_session.rollback()
    cls_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_after.available_seats == 20


def test_cancellation_transaction_failure_rollback(client, db_session, monkeypatch):
    """
    Simulate an error during cancellation before commit.
    Verify:
    - Transaction rolls back completely
    - Seat remains BOOKED
    - Class available_seats remains unchanged
    - Booking status remains CONFIRMED
    - No orphan refund record exists
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, passenger_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_rollback_c")
    flight_id, _ = create_test_flight_with_custom_fares(client, ops_token, flight_number="PK907")

    booking = create_and_confirm_booking(client, passenger_token, flight_id, count=1)
    booking_id = UUID(booking["id"])
    seat_id = UUID(booking["items"][0]["seat_id"])

    # Monkeypatch db.commit to raise an error
    original_commit = Session.commit

    def mock_commit_error(self, *args, **kwargs):
        raise RuntimeError("Simulated crash right before cancellation commit")

    monkeypatch.setattr(Session, "commit", mock_commit_error)

    from app.schemas.refund import BookingCancellationRequest
    from app.services.cancellation_service import cancel_booking_or_items

    with pytest.raises(RuntimeError, match="Simulated crash right before cancellation commit"):
        cancel_booking_or_items(
            db=db_session,
            booking_id=booking_id,
            payload=BookingCancellationRequest(),
            current_user=passenger,
        )

    # Rollback session as the application framework does on unhandled exception
    db_session.rollback()

    # Verify rollback: booking is STILL CONFIRMED
    booking_in_db = db_session.query(Booking).filter(Booking.id == booking_id).first()
    assert booking_in_db.status == BookingStatus.CONFIRMED

    # Seat is STILL BOOKED
    seat_in_db = db_session.query(FlightSeat).filter(FlightSeat.id == seat_id).first()
    assert seat_in_db.status == SeatStatus.BOOKED

    # Available seats remained 19
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 19

    # No refund record was saved
    refunds = db_session.query(Refund).filter(Refund.booking_id == booking_id).all()
    assert len(refunds) == 0
