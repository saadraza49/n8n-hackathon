from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Tuple
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.models.booking import Booking, BookingItem
from app.models.enums import BookingStatus, FareType, FlightClassType, FlightStatus, SeatStatus, UserRole
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


def create_test_flight_with_fares(client, ops_token: str) -> Tuple[str, dict]:
    """Helper to create a flight and configure basic & flexible fare rules."""
    dep_date = date(2026, 10, 15)
    dep_at = datetime(dep_date.year, dep_date.month, dep_date.day, 10, 0, 0, tzinfo=timezone.utc)
    arr_at = dep_at + timedelta(hours=6)

    flight_res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "flight_number": f"PK{uuid4().hex[:3].upper()}",
            "origin": "ISB",
            "destination": "LHR",
            "departure_at": dep_at.isoformat(),
            "arrival_at": arr_at.isoformat(),
            "total_capacity": 60,
            "first_class_seats": 10,
            "business_class_seats": 20,
            "economy_class_seats": 30,
        },
    )
    assert flight_res.status_code == 201
    flight_id = flight_res.json()["id"]

    # Add ECONOMY BASIC fare (seat_selection_allowed = False)
    res_basic = client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "BASIC",
            "price": "150.00",
            "currency": "USD",
            "changes_allowed": False,
            "seat_selection_allowed": False,
            "refundable": False,
            "credit_only": False,
            "cancellation_cutoff_minutes": 120,
        },
    )
    assert res_basic.status_code == 201

    # Add ECONOMY FLEXIBLE fare (seat_selection_allowed = True)
    res_flex = client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "FLEXIBLE",
            "price": "220.00",
            "currency": "USD",
            "changes_allowed": True,
            "seat_selection_allowed": True,
            "refundable": True,
            "credit_only": False,
            "cancellation_cutoff_minutes": 60,
        },
    )
    assert res_flex.status_code == 201

    return flight_id, {"basic_price": Decimal("150.00"), "flex_price": Decimal("220.00")}


# ==========================================
# 1. SEAT HOLD TESTS
# ==========================================

def test_hold_available_seat_success(client, db_session):
    """Verify an available seat can be held, setting status to HELD, creating PENDING booking, and decrementing available_seats."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    user, passenger_token = create_user_with_role(db_session, UserRole.PASSENGER, "p1")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    # Get a physical economy seat
    seat = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.class_type == FlightClassType.ECONOMY,
        FlightSeat.status == SeatStatus.AVAILABLE,
    ).first()
    assert seat is not None

    res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {passenger_token}"},
        json={
            "flight_id": flight_id,
            "items": [
                {
                    "seat_id": str(seat.id),
                    "class_type": "ECONOMY",
                    "fare_type": "FLEXIBLE",
                    "passenger_name": "John Doe",
                }
            ],
            "hold_duration_minutes": 15,
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "PENDING"
    assert data["total_amount"] == 220.0 or Decimal(str(data["total_amount"])) == Decimal("220.00")
    assert len(data["items"]) == 1
    assert data["items"][0]["seat_number"] == seat.seat_number
    assert data["items"][0]["passenger_name"] == "John Doe"

    # Verify DB state
    db_session.expire_all()
    refreshed_seat = db_session.query(FlightSeat).filter(FlightSeat.id == seat.id).first()
    assert refreshed_seat.status == SeatStatus.HELD
    assert refreshed_seat.hold_expires_at is not None

    flight_class = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert flight_class.available_seats == 29  # 30 initial - 1 held


def test_hold_auto_seat_assignment(client, db_session):
    """Verify seat_id can be omitted and an available seat is automatically assigned."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, passenger_token = create_user_with_role(db_session, UserRole.PASSENGER, "p2")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {passenger_token}"},
        json={
            "flight_id": flight_id,
            "items": [
                {
                    "class_type": "ECONOMY",
                    "fare_type": "BASIC",
                    "passenger_name": "Jane Doe",
                }
            ],
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "PENDING"
    assigned_seat_number = data["items"][0]["seat_number"]
    assert assigned_seat_number is not None

    # Verify DB
    db_session.expire_all()
    assigned_seat = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.seat_number == assigned_seat_number,
    ).first()
    assert assigned_seat.status == SeatStatus.HELD


def test_hold_seat_selection_not_allowed_for_fare_fails(client, db_session):
    """Verify client cannot choose a specific seat if fare rule prohibits seat selection."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, passenger_token = create_user_with_role(db_session, UserRole.PASSENGER, "p3")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    seat = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.class_type == FlightClassType.ECONOMY,
    ).first()

    # BASIC fare has seat_selection_allowed=False
    res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {passenger_token}"},
        json={
            "flight_id": flight_id,
            "items": [
                {
                    "seat_id": str(seat.id),
                    "class_type": "ECONOMY",
                    "fare_type": "BASIC",
                }
            ],
        },
    )
    assert res.status_code == 400
    assert "Seat selection is not permitted" in res.json()["detail"]


def test_hold_already_held_seat_fails_409(client, db_session):
    """Verify holding a seat already held by another user returns 409 Conflict."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p4_1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p4_2")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    seat = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.class_type == FlightClassType.ECONOMY,
    ).first()

    # User 1 holds seat
    res1 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p1_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"seat_id": str(seat.id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res1.status_code == 201

    # User 2 tries to hold same seat
    res2 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p2_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"seat_id": str(seat.id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res2.status_code == 409
    assert "currently held" in res2.json()["detail"]


def test_hold_booked_or_blocked_seat_fails_409(client, db_session):
    """Verify BOOKED or BLOCKED seats cannot be held."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p5")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    seats = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.class_type == FlightClassType.ECONOMY,
    ).limit(2).all()

    # Mark first BOOKED and second BLOCKED
    seats[0].status = SeatStatus.BOOKED
    seats[1].status = SeatStatus.BLOCKED
    db_session.commit()

    # Try BOOKED
    res1 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"seat_id": str(seats[0].id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res1.status_code == 409

    # Try BLOCKED
    res2 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"seat_id": str(seats[1].id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res2.status_code == 409


def test_reclaim_expired_hold_success(client, db_session):
    """Verify an expired hold can be reclaimed by another user without double-decrementing inventory."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p6_1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p6_2")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    seat = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.class_type == FlightClassType.ECONOMY,
    ).first()

    # User 1 holds seat
    res1 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p1_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"seat_id": str(seat.id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res1.status_code == 201

    # Force seat hold to expire in database
    db_session.expire_all()
    seat = db_session.query(FlightSeat).filter(FlightSeat.id == seat.id).first()
    seat.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    # Check inventory before User 2's request
    cls_before = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    avail_before = cls_before.available_seats

    # User 2 holds the seat now that hold has expired
    res2 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p2_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"seat_id": str(seat.id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res2.status_code == 201

    # Inventory must NOT be decremented again because the seat was already counted as held
    db_session.expire_all()
    cls_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_after.available_seats == avail_before


# ==========================================
# 2. BOOKING CONFIRMATION TESTS
# ==========================================

def test_confirm_booking_success(client, db_session):
    """Verify valid hold confirms to BOOKED without double-decrementing inventory."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p7")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    # 1. Hold seat
    res_hold = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res_hold.status_code == 201
    booking_id = res_hold.json()["id"]

    db_session.expire_all()
    flight_class = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert flight_class.available_seats == 29

    # 2. Confirm booking
    res_confirm = client.post(
        f"/bookings/{booking_id}/confirm",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert res_confirm.status_code == 200
    confirm_data = res_confirm.json()
    assert confirm_data["status"] == "CONFIRMED"

    # 3. Check DB state: seat is BOOKED, available_seats is STILL 29 (NOT 28!)
    db_session.expire_all()
    booking = db_session.query(Booking).filter(Booking.id == UUID(booking_id)).first()
    assert booking.status == BookingStatus.CONFIRMED

    booked_seat_id = booking.items[0].seat_id
    booked_seat = db_session.query(FlightSeat).filter(FlightSeat.id == booked_seat_id).first()
    assert booked_seat.status == SeatStatus.BOOKED
    assert booked_seat.hold_expires_at is None

    flight_class_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert flight_class_after.available_seats == 29  # INVARIANT: Not decremented twice!


def test_confirm_expired_hold_fails_and_releases_seat(client, db_session):
    """Verify confirmation of an expired hold fails and automatically restores inventory."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p8")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    res_hold = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    assert res_hold.status_code == 201
    booking_id = res_hold.json()["id"]

    # Expire the hold in database
    db_session.expire_all()
    booking = db_session.query(Booking).filter(Booking.id == UUID(booking_id)).first()
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    for it in booking.items:
        it.seat.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.commit()

    # Attempt to confirm expired booking
    res_confirm = client.post(
        f"/bookings/{booking_id}/confirm",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert res_confirm.status_code == 400
    assert "Hold has expired" in res_confirm.json()["detail"]

    # Verify DB: booking is EXPIRED, seat is AVAILABLE, class inventory restored to 30
    db_session.expire_all()
    refreshed_booking = db_session.query(Booking).filter(Booking.id == UUID(booking_id)).first()
    assert refreshed_booking.status == BookingStatus.EXPIRED

    seat = refreshed_booking.items[0].seat
    assert seat.status == SeatStatus.AVAILABLE
    assert seat.hold_expires_at is None

    flight_class = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert flight_class.available_seats == 30  # Restored!


def test_confirm_by_another_passenger_forbidden(client, db_session):
    """Verify passenger 2 cannot confirm passenger 1's booking hold."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p9_1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p9_2")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    res_hold = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p1_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    booking_id = res_hold.json()["id"]

    # Passenger 2 attempts confirmation
    res_confirm = client.post(
        f"/bookings/{booking_id}/confirm",
        headers={"Authorization": f"Bearer {p2_token}"},
    )
    assert res_confirm.status_code == 403


# ==========================================
# 3. FARE SNAPSHOT & SEAT SELECTION TESTS
# ==========================================

def test_price_snapshot_preserved_on_fare_rule_change(client, db_session):
    """Verify changing fare rule prices later does not affect confirmed booking's price snapshot."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p10")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    # 1. Hold and confirm with current price (220.00)
    res_hold = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "flight_id": flight_id,
            "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
        },
    )
    booking_id = res_hold.json()["id"]
    client.post(f"/bookings/{booking_id}/confirm", headers={"Authorization": f"Bearer {p_token}"})

    # 2. Ops updates the fare rule price from 220.00 -> 350.00
    res_fares = client.get(f"/flights/{flight_id}/fares", headers={"Authorization": f"Bearer {ops_token}"})
    flex_rule = [r for r in res_fares.json() if r["fare_type"] == "FLEXIBLE"][0]
    client.patch(
        f"/flights/{flight_id}/fares/{flex_rule['id']}",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"price": "350.00"},
    )

    # 3. Retrieve booking: must STILL be 220.00
    res_booking = client.get(f"/bookings/{booking_id}", headers={"Authorization": f"Bearer {p_token}"})
    assert res_booking.status_code == 200
    booking_data = res_booking.json()
    assert booking_data["total_amount"] == 220.0 or Decimal(str(booking_data["total_amount"])) == Decimal("220.00")
    assert booking_data["items"][0]["price"] == 220.0 or Decimal(str(booking_data["items"][0]["price"])) == Decimal("220.00")


# ==========================================
# 4. IDEMPOTENCY TESTS
# ==========================================

def test_idempotency_exact_retry_returns_same_booking(client, db_session):
    """Verify repeating the exact same booking hold with the same idempotency key returns previous booking."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p11")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    payload = {
        "flight_id": flight_id,
        "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE", "passenger_name": "Alice"}],
        "idempotency_key": "unique-idem-12345",
    }

    # Request 1
    res1 = client.post("/bookings/holds", headers={"Authorization": f"Bearer {p_token}"}, json=payload)
    assert res1.status_code == 201
    booking_id1 = res1.json()["id"]

    # Request 2 (identical retry)
    res2 = client.post("/bookings/holds", headers={"Authorization": f"Bearer {p_token}"}, json=payload)
    assert res2.status_code in (200, 201)
    booking_id2 = res2.json()["id"]

    assert booking_id1 == booking_id2

    # Verify available_seats was only decremented ONCE
    db_session.expire_all()
    flight_class = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert flight_class.available_seats == 29


def test_idempotency_different_payload_fails_409(client, db_session):
    """Verify reusing an idempotency key with a materially different payload is rejected with 409 Conflict."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p12")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    payload1 = {
        "flight_id": flight_id,
        "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE", "passenger_name": "Alice"}],
        "idempotency_key": "unique-idem-diff",
    }
    res1 = client.post("/bookings/holds", headers={"Authorization": f"Bearer {p_token}"}, json=payload1)
    assert res1.status_code == 201

    # Different payload with same key
    payload2 = {
        "flight_id": flight_id,
        "items": [{"class_type": "ECONOMY", "fare_type": "BASIC", "passenger_name": "Bob"}],
        "idempotency_key": "unique-idem-diff",
    }
    res2 = client.post("/bookings/holds", headers={"Authorization": f"Bearer {p_token}"}, json=payload2)
    assert res2.status_code == 409
    assert "Idempotency key reused" in res2.json()["detail"]


# ==========================================
# 5. RETRIEVAL & LIST RBAC TESTS
# ==========================================

def test_booking_retrieval_and_list_rbac(client, db_session):
    """Verify passenger isolation in booking retrieval and listing, and ops/admin full access."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p13_1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p13_2")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    # Passenger 1 creates booking
    res1 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p1_token}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}]},
    )
    b1_id = res1.json()["id"]

    # Passenger 2 creates booking
    res2 = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p2_token}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "BASIC"}]},
    )
    b2_id = res2.json()["id"]

    # Passenger 1 gets own booking -> 200 OK
    assert client.get(f"/bookings/{b1_id}", headers={"Authorization": f"Bearer {p1_token}"}).status_code == 200

    # Passenger 1 attempts to get Passenger 2's booking -> 403 Forbidden
    assert client.get(f"/bookings/{b2_id}", headers={"Authorization": f"Bearer {p1_token}"}).status_code == 403

    # Passenger 1 lists bookings -> sees only 1
    p1_list = client.get("/bookings", headers={"Authorization": f"Bearer {p1_token}"}).json()
    assert p1_list["pagination"]["total"] == 1
    assert p1_list["items"][0]["id"] == b1_id

    # Ops lists bookings -> sees both
    ops_list = client.get("/bookings", headers={"Authorization": f"Bearer {ops_token}"}).json()
    assert ops_list["pagination"]["total"] == 2


# ==========================================
# 6. BOOKING CANCELLATION TESTS
# ==========================================

def test_cancel_booking_releases_inventory(client, db_session):
    """Verify cancelling a booking transitions seats to AVAILABLE and restores available_seats."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p14")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    # Hold and confirm
    res_hold = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_token}"},
        json={"flight_id": flight_id, "items": [{"class_type": "ECONOMY", "fare_type": "FLEXIBLE"}]},
    )
    b_id = res_hold.json()["id"]
    client.post(f"/bookings/{b_id}/confirm", headers={"Authorization": f"Bearer {p_token}"})

    # Available seats should be 29
    db_session.expire_all()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 29

    # Cancel booking
    res_cancel = client.post(f"/bookings/{b_id}/cancel", headers={"Authorization": f"Bearer {p_token}"})
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] == "CANCELLED"

    # Verify DB: seat is AVAILABLE, available_seats restored to 30
    db_session.expire_all()
    booking = db_session.query(Booking).filter(Booking.id == UUID(b_id)).first()
    assert booking.status == BookingStatus.CANCELLED

    seat = booking.items[0].seat
    assert seat.status == SeatStatus.AVAILABLE

    cls_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_after.available_seats == 30


# ==========================================
# 7. MULTI-SEAT / GROUP BOOKING ATOMICITY
# ==========================================

def test_group_booking_atomic_hold(client, db_session):
    """Verify holding multiple seats succeeds atomically and decrements inventory appropriately."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p15")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    res = client.post(
        "/bookings/holds",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "flight_id": flight_id,
            "items": [
                {"class_type": "ECONOMY", "fare_type": "BASIC", "passenger_name": "Passenger 1"},
                {"class_type": "ECONOMY", "fare_type": "BASIC", "passenger_name": "Passenger 2"},
                {"class_type": "ECONOMY", "fare_type": "BASIC", "passenger_name": "Passenger 3"},
            ],
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert len(data["items"]) == 3
    # Check that each item got a distinct physical seat
    assigned_seats = [it["seat_id"] for it in data["items"]]
    assert len(set(assigned_seats)) == 3

    # Inventory must have decremented by exactly 3
    db_session.expire_all()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 27


# ==========================================
# 8. CONCURRENCY & RACE CONDITION TEST
# ==========================================

def test_concurrency_race_condition_on_same_seat(client, db_session):
    """
    Verify concurrency safety: when two concurrent threads attempt to hold the EXACT
    same physical seat simultaneously, exactly ONE succeeds with 201 and the other
    fails cleanly with 409 Conflict.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_race_1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_race_2")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    seat = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.class_type == FlightClassType.ECONOMY,
        FlightSeat.status == SeatStatus.AVAILABLE,
    ).first()
    assert seat is not None

    payload = {
        "flight_id": flight_id,
        "items": [{"seat_id": str(seat.id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
    }

    def attempt_hold(token: str):
        return client.post("/bookings/holds", headers={"Authorization": f"Bearer {token}"}, json=payload)

    # Launch both concurrent attempts simultaneously
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_hold, p1_token)
        f2 = executor.submit(attempt_hold, p2_token)
        res1 = f1.result()
        res2 = f2.result()

    status_codes = sorted([res1.status_code, res2.status_code])
    # Exactly one must succeed (201) and one must fail with conflict (409 or 400)
    assert status_codes in ([201, 409], [201, 400])

    # Invariant: available_seats decrements exactly by 1 (from 30 to 29)
    db_session.rollback()
    db_session.expire_all()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 29


# ==========================================
# 9. TRANSACTION FAILURE ROLLBACK TEST
# ==========================================

def test_transaction_failure_rollback(client, db_session, monkeypatch):
    """
    Simulate a failure after inventory modification but before commit.
    Verify:
    - Transaction rolls back completely
    - Seat returns to previous state (AVAILABLE)
    - available_seats returns to previous value (30)
    - No orphan booking remains in the database
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, passenger_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_rollback")
    flight_id, _ = create_test_flight_with_fares(client, ops_token)

    seat = db_session.query(FlightSeat).filter(
        FlightSeat.flight_id == UUID(flight_id),
        FlightSeat.class_type == FlightClassType.ECONOMY,
        FlightSeat.status == SeatStatus.AVAILABLE,
    ).first()
    assert seat is not None
    seat_id = seat.id

    # Monkeypatch db.flush to simulate a database or network crash right after seat/class modifications
    original_flush = Session.flush

    def mock_flush_error(self, *args, **kwargs):
        raise RuntimeError("Simulated database failure during transaction execution")

    monkeypatch.setattr(Session, "flush", mock_flush_error)

    payload = {
        "flight_id": flight_id,
        "items": [{"seat_id": str(seat_id), "class_type": "ECONOMY", "fare_type": "FLEXIBLE"}],
    }

    with pytest.raises(RuntimeError, match="Simulated database failure"):
        from app.schemas.booking import BookingHoldCreate
        from app.services.booking_service import create_hold
        create_hold(db=db_session, payload=BookingHoldCreate(**payload), current_user=passenger)

    # Rollback session as the application/framework does on unhandled exception
    db_session.rollback()
    db_session.expire_all()

    # Verify rollback: seat is still AVAILABLE
    refreshed_seat = db_session.query(FlightSeat).filter(FlightSeat.id == seat_id).first()
    assert refreshed_seat.status == SeatStatus.AVAILABLE
    assert refreshed_seat.hold_expires_at is None

    # Verify rollback: available_seats remains 30
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 30

    # Verify rollback: no orphan bookings exist
    orphan_bookings = db_session.query(Booking).filter(Booking.user_id == passenger.id).all()
    assert len(orphan_bookings) == 0


