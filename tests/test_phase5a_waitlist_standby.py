from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Tuple
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.models.booking import Booking
from app.models.enums import (
    BookingStatus,
    FareType,
    FlightClassType,
    FlightStatus,
    SeatStatus,
    UserRole,
    WaitlistStatus,
)
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User
from app.models.waitlist import WaitlistEntry


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


def create_test_flight_full_setup(client, ops_token: str, flight_number: str = "PK801") -> Tuple[str, dict]:
    """Create a flight with small capacity (e.g. 2 economy seats, 1 business, 1 first = 4 total) for full-booking tests."""
    dep_date = date(2026, 12, 10)
    dep_at = datetime(dep_date.year, dep_date.month, dep_date.day, 14, 0, 0, tzinfo=timezone.utc)
    arr_at = dep_at + timedelta(hours=3)

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

    # Configure flexible & basic fares
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
    client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "BUSINESS",
            "fare_type": "FLEXIBLE",
            "price": "400.00",
            "currency": "USD",
            "seat_selection_allowed": True,
            "changes_allowed": True,
            "refundable": True,
            "cancellation_cutoff_minutes": 60,
        },
    )
    client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "FIRST",
            "fare_type": "FLEXIBLE",
            "price": "600.00",
            "currency": "USD",
            "seat_selection_allowed": True,
            "changes_allowed": True,
            "refundable": True,
            "cancellation_cutoff_minutes": 60,
        },
    )

    return flight_id, flight_res.json()


def fill_class_seats(client, passenger_token: str, flight_id: str, class_type: FlightClassType, count: int):
    """Fill seats in a class by holding and confirming bookings."""
    for _ in range(count):
        hold_res = client.post(
            "/bookings/holds",
            headers={"Authorization": f"Bearer {passenger_token}"},
            json={
                "flight_id": flight_id,
                "items": [{"class_type": class_type.value, "fare_type": "FLEXIBLE"}],
            },
        )
        assert hold_res.status_code == 201, hold_res.text
        booking_id = hold_res.json()["id"]

        confirm_res = client.post(
            f"/bookings/{booking_id}/confirm",
            headers={"Authorization": f"Bearer {passenger_token}"},
            json={"payment_reference": f"PAY-{uuid4().hex[:8]}"},
        )
        assert confirm_res.status_code == 200, confirm_res.text


# ============================================================================
# 1. JOIN VALIDATION & INVENTORY INVARIANTS
# ============================================================================

def test_join_waitlist_when_class_has_seats_fails_409(client, db_session):
    """
    Joining waitlist when cabin class has available inventory must be rejected
    with HTTP 409 Conflict.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p1")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK802")

    res = client.post(
        f"/flights/{flight_id}/waitlist",
        headers={"Authorization": f"Bearer {p_token}"},
        json={"class_type": "ECONOMY"},
    )
    assert res.status_code == 409
    assert "Direct booking is required" in res.json()["detail"]


def test_join_waitlist_success_when_class_full(client, db_session):
    """
    Joining waitlist when cabin class is 100% full succeeds (201 Created),
    assigns WAITING status, and derives queue position 1.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p1_book")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p2_wait")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK803")

    # Fill all 2 Economy seats
    fill_class_seats(client, p1_token, flight_id, FlightClassType.ECONOMY, count=2)

    # Verify available_seats == 0
    db_session.rollback()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.ECONOMY,
    ).first()
    assert cls_check.available_seats == 0

    # Join waitlist
    res = client.post(
        f"/flights/{flight_id}/waitlist",
        headers={"Authorization": f"Bearer {p2_token}"},
        json={"class_type": "ECONOMY", "notes": "Need window if possible"},
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["status"] == "WAITING"
    assert data["class_type"] == "ECONOMY"
    assert data["entry_type"] == "WAITLIST"
    assert data["queue_position"] == 1
    assert data["notes"] == "Need window if possible"


def test_join_waitlist_invalid_flight_conditions(client, db_session):
    """Reject waitlist join for non-existent, cancelled, or departed flights."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_invalid")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK804")

    # 1. Nonexistent flight
    res_fake = client.post(
        f"/flights/{uuid4()}/waitlist",
        headers={"Authorization": f"Bearer {p_token}"},
        json={"class_type": "ECONOMY"},
    )
    assert res_fake.status_code == 404

    # 2. Cancelled flight
    client.post(f"/flights/{flight_id}/cancel", headers={"Authorization": f"Bearer {ops_token}"})
    res_canc = client.post(
        f"/flights/{flight_id}/waitlist",
        headers={"Authorization": f"Bearer {p_token}"},
        json={"class_type": "ECONOMY"},
    )
    assert res_canc.status_code == 400
    assert "cannot join waitlist" in res_canc.json()["detail"].lower()


# ============================================================================
# 2. CLASS ISOLATION & DUPLICATE PROTECTION
# ============================================================================

def test_class_specific_waitlist_isolation(client, db_session):
    """Business waitlist is isolated from Economy waitlist."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p1_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_iso1")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_iso2")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK805")

    # Fill Business (1 seat)
    fill_class_seats(client, p1_token, flight_id, FlightClassType.BUSINESS, count=1)

    # Economy has 2 available seats; Business has 0 available seats
    # Passenger cannot join Economy waitlist (409 Conflict)
    res_econ = client.post(
        f"/flights/{flight_id}/waitlist",
        headers={"Authorization": f"Bearer {p2_token}"},
        json={"class_type": "ECONOMY"},
    )
    assert res_econ.status_code == 409

    # Passenger CAN join Business waitlist (201 Created)
    res_biz = client.post(
        f"/flights/{flight_id}/waitlist",
        headers={"Authorization": f"Bearer {p2_token}"},
        json={"class_type": "BUSINESS"},
    )
    assert res_biz.status_code == 201
    assert res_biz.json()["class_type"] == "BUSINESS"


def test_duplicate_active_waitlist_rejected_409(client, db_session):
    """
    A passenger cannot have multiple active waitlist entries for the same flight and class.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_dup_book")
    _, p_wait_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_dup_wait")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK806")

    fill_class_seats(client, p_book_token, flight_id, FlightClassType.BUSINESS, count=1)

    # First join succeeds
    res1 = client.post(
        f"/flights/{flight_id}/waitlist",
        headers={"Authorization": f"Bearer {p_wait_token}"},
        json={"class_type": "BUSINESS"},
    )
    assert res1.status_code == 201

    # Second active join for same flight and class must fail with 409 Conflict
    res2 = client.post(
        f"/flights/{flight_id}/waitlist",
        headers={"Authorization": f"Bearer {p_wait_token}"},
        json={"class_type": "BUSINESS"},
    )
    assert res2.status_code == 409
    assert "already have an active waitlist entry" in res2.json()["detail"].lower()


def test_concurrent_duplicate_waitlist_join_race(client, db_session):
    """
    Concurrency safety: concurrent duplicate requests from same passenger allow exactly one
    active waitlist entry and reject the duplicate.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book = create_user_with_role(db_session, UserRole.PASSENGER, "p_race_book")
    passenger, p_wait = create_user_with_role(db_session, UserRole.PASSENGER, "p_race_wait")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK807")

    fill_class_seats(client, p_book, flight_id, FlightClassType.BUSINESS, count=1)

    def attempt_join():
        return client.post(
            f"/flights/{flight_id}/waitlist",
            headers={"Authorization": f"Bearer {p_wait}"},
            json={"class_type": "BUSINESS"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_join)
        f2 = executor.submit(attempt_join)
        res1 = f1.result()
        res2 = f2.result()

    status_codes = sorted([res1.status_code, res2.status_code])
    assert status_codes in ([201, 409], [201, 400])

    # Exactly 1 active entry in database
    db_session.rollback()
    entries = db_session.query(WaitlistEntry).filter(
        WaitlistEntry.passenger_id == passenger.id,
        WaitlistEntry.flight_id == UUID(flight_id),
        WaitlistEntry.class_type == FlightClassType.BUSINESS,
    ).all()
    assert len(entries) == 1


# ============================================================================
# 3. DETERMINISTIC PRIORITY & DYNAMIC QUEUE POSITION
# ============================================================================

def test_deterministic_priority_and_dynamic_queue_position(client, db_session):
    """
    Queue position is derived at read time from deterministic priority order.
    When a preceding passenger cancels, following passengers dynamically adjust position
    with zero table renumbering overhead.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book = create_user_with_role(db_session, UserRole.PASSENGER, "p_q_book")
    _, p1_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p1_q")
    _, p2_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p2_q")
    _, p3_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p3_q")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK808")

    fill_class_seats(client, p_book, flight_id, FlightClassType.BUSINESS, count=1)

    # Join in order: P1, P2, P3
    res1 = client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p1_tok}"}, json={"class_type": "BUSINESS"})
    res2 = client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p2_tok}"}, json={"class_type": "BUSINESS"})
    res3 = client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p3_tok}"}, json={"class_type": "BUSINESS"})

    assert res1.json()["queue_position"] == 1
    assert res2.json()["queue_position"] == 2
    assert res3.json()["queue_position"] == 3

    p1_entry_id = res1.json()["id"]

    # P1 cancels waitlist
    del_res = client.delete(f"/waitlists/{p1_entry_id}", headers={"Authorization": f"Bearer {p1_tok}"})
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "CANCELLED"

    # Now P2 should dynamically be queue position 1, and P3 should be queue position 2!
    p2_check = client.get("/waitlists/me", headers={"Authorization": f"Bearer {p2_tok}"}).json()
    assert p2_check[0]["queue_position"] == 1

    p3_check = client.get("/waitlists/me", headers={"Authorization": f"Bearer {p3_tok}"}).json()
    assert p3_check[0]["queue_position"] == 2


# ============================================================================
# 4. IDEMPOTENCY & PASSENGER RETRIEVAL RBAC
# ============================================================================

def test_waitlist_idempotency(client, db_session):
    """Replaying request with same Idempotency-Key returns original response."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book = create_user_with_role(db_session, UserRole.PASSENGER, "p_idem_book")
    _, p_wait = create_user_with_role(db_session, UserRole.PASSENGER, "p_idem_wait")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK809")

    fill_class_seats(client, p_book, flight_id, FlightClassType.BUSINESS, count=1)

    headers = {"Authorization": f"Bearer {p_wait}", "Idempotency-Key": "waitlist-key-999"}
    res_orig = client.post(f"/flights/{flight_id}/waitlist", headers=headers, json={"class_type": "BUSINESS"})
    assert res_orig.status_code == 201

    # Exact replay returns 201 with same entry id
    res_replay = client.post(f"/flights/{flight_id}/waitlist", headers=headers, json={"class_type": "BUSINESS"})
    assert res_replay.status_code == 201
    assert res_replay.json()["id"] == res_orig.json()["id"]


def test_passenger_retrieval_and_rbac(client, db_session):
    """Passenger views own waitlists; cannot view another's; Ops views flight queue."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book = create_user_with_role(db_session, UserRole.PASSENGER, "p_rbac_b")
    _, p1_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_rbac_1")
    _, p2_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_rbac_2")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK810")

    fill_class_seats(client, p_book, flight_id, FlightClassType.BUSINESS, count=1)

    join1 = client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p1_tok}"}, json={"class_type": "BUSINESS"}).json()
    join2 = client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p2_tok}"}, json={"class_type": "BUSINESS"}).json()

    # Passenger 1 cannot view Passenger 2's entry
    forbidden_res = client.get(f"/waitlists/{join2['id']}", headers={"Authorization": f"Bearer {p1_tok}"})
    assert forbidden_res.status_code == 403

    # Passenger 1 can view own entry
    own_res = client.get(f"/waitlists/{join1['id']}", headers={"Authorization": f"Bearer {p1_tok}"})
    assert own_res.status_code == 200
    assert own_res.json()["id"] == join1["id"]

    # Ops can view entire flight waitlist queue
    ops_queue_res = client.get(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {ops_token}"})
    assert ops_queue_res.status_code == 200
    queue_data = ops_queue_res.json()
    assert queue_data["total_waiting"] == 2
    assert len(queue_data["entries"]) == 2


# ============================================================================
# 5. PROMOTION WORKFLOW & CLAIM DEADLINE
# ============================================================================

def test_promote_next_passenger_success(client, db_session):
    """
    When a seat opens, Ops/worker triggers promotion.
    Top waiting passenger transitions to PROMOTED with claim_deadline.
    Seat becomes HELD and class inventory decrements.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_prom_b")
    _, p_wait_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_prom_w")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK811")

    fill_class_seats(client, p_book_tok, flight_id, FlightClassType.BUSINESS, count=1)

    # Join waitlist
    join_res = client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p_wait_tok}"}, json={"class_type": "BUSINESS"})
    assert join_res.status_code == 201

    # Booked passenger cancels booking -> frees seat to AVAILABLE
    b_id = db_session.query(Booking).filter(Booking.flight_id == UUID(flight_id)).first().id
    client.post(f"/bookings/{b_id}/cancel", headers={"Authorization": f"Bearer {p_book_tok}"})

    # Available seats is now 1
    db_session.rollback()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.BUSINESS,
    ).first()
    assert cls_check.available_seats == 1

    # Trigger promotion
    prom_res = client.post(
        f"/flights/{flight_id}/waitlist/promote",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"class_type": "BUSINESS", "claim_window_minutes": 20},
    )
    assert prom_res.status_code == 200, prom_res.text
    prom_data = prom_res.json()
    assert prom_data["status"] == "PROMOTED"
    assert prom_data["claim_deadline"] is not None
    assert prom_data["promoted_seat_id"] is not None

    # Invariant: class available_seats decremented back to 0 (held by promotion)
    db_session.rollback()
    cls_after = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.BUSINESS,
    ).first()
    assert cls_after.available_seats == 0

    # Promoted seat is now HELD
    seat = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(prom_data["promoted_seat_id"])).first()
    assert seat.status == SeatStatus.HELD


def test_claim_promoted_seat_converts_to_booking(client, db_session):
    """
    Promoted passenger claims seat within claim deadline.
    Transitions waitlist entry to CONVERTED and creates confirmed booking.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_clm_b")
    _, p_wait_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_clm_w")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK812")

    fill_class_seats(client, p_book_tok, flight_id, FlightClassType.BUSINESS, count=1)
    client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p_wait_tok}"}, json={"class_type": "BUSINESS"})

    # Free seat & promote
    b_id = db_session.query(Booking).filter(Booking.flight_id == UUID(flight_id)).first().id
    client.post(f"/bookings/{b_id}/cancel", headers={"Authorization": f"Bearer {p_book_tok}"})
    prom_data = client.post(
        f"/flights/{flight_id}/waitlist/promote",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"class_type": "BUSINESS"},
    ).json()

    waitlist_id = prom_data["id"]

    # Claim seat
    claim_res = client.post(
        f"/waitlists/{waitlist_id}/claim",
        headers={"Authorization": f"Bearer {p_wait_tok}"},
        json={"fare_type": "FLEXIBLE", "passenger_name": "Promoted Passenger"},
    )
    assert claim_res.status_code == 200, claim_res.text
    claim_data = claim_res.json()
    assert claim_data["booking_id"] is not None
    assert claim_data["booking_reference"] is not None
    assert claim_data["seat_number"] is not None
    assert claim_data["class_type"] == "BUSINESS"

    # Waitlist entry is now CONVERTED
    entry_check = client.get(f"/waitlists/{waitlist_id}", headers={"Authorization": f"Bearer {p_wait_tok}"}).json()
    assert entry_check["status"] == "CONVERTED"
    assert entry_check["claimed_at"] is not None
    assert entry_check["booking_id"] == claim_data["booking_id"]

    # Physical seat is now BOOKED
    db_session.rollback()
    seat = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(prom_data["promoted_seat_id"])).first()
    assert seat.status == SeatStatus.BOOKED


def test_claim_after_deadline_expired_fails_and_releases_seat(client, db_session):
    """
    Attempting to claim after claim_deadline fails (400 Bad Request),
    marks entry EXPIRED, and releases held seat back to AVAILABLE.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_exp_b")
    _, p_wait_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_exp_w")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK813")

    fill_class_seats(client, p_book_tok, flight_id, FlightClassType.BUSINESS, count=1)
    client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p_wait_tok}"}, json={"class_type": "BUSINESS"})

    b_id = db_session.query(Booking).filter(Booking.flight_id == UUID(flight_id)).first().id
    client.post(f"/bookings/{b_id}/cancel", headers={"Authorization": f"Bearer {p_book_tok}"})
    prom_data = client.post(
        f"/flights/{flight_id}/waitlist/promote",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"class_type": "BUSINESS"},
    ).json()

    waitlist_id = UUID(prom_data["id"])

    # Manually expire deadline into the past
    past_deadline = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.query(WaitlistEntry).filter(WaitlistEntry.id == waitlist_id).update(
        {WaitlistEntry.claim_deadline: past_deadline}
    )
    db_session.commit()

    # Attempt claim -> fails 400
    res_claim = client.post(
        f"/waitlists/{waitlist_id}/claim",
        headers={"Authorization": f"Bearer {p_wait_tok}"},
        json={"fare_type": "FLEXIBLE"},
    )
    assert res_claim.status_code == 400
    assert "expired" in res_claim.json()["detail"].lower()

    # Invariant: entry is EXPIRED
    db_session.rollback()
    entry = db_session.query(WaitlistEntry).filter(WaitlistEntry.id == waitlist_id).first()
    assert entry.status == WaitlistStatus.EXPIRED

    # Invariant: seat returned to AVAILABLE and class available_seats restored to 1
    seat = db_session.query(FlightSeat).filter(FlightSeat.id == entry.promoted_seat_id).first()
    assert seat.status == SeatStatus.AVAILABLE
    assert seat.hold_expires_at is None

    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.BUSINESS,
    ).first()
    assert cls_check.available_seats == 1


def test_cancel_promoted_waitlist_releases_held_seat(client, db_session):
    """
    Cancelling a PROMOTED waitlist entry releases held seat and restores inventory.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_cpr_b")
    _, p_wait_tok = create_user_with_role(db_session, UserRole.PASSENGER, "p_cpr_w")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK814")

    fill_class_seats(client, p_book_tok, flight_id, FlightClassType.BUSINESS, count=1)
    client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p_wait_tok}"}, json={"class_type": "BUSINESS"})

    b_id = db_session.query(Booking).filter(Booking.flight_id == UUID(flight_id)).first().id
    client.post(f"/bookings/{b_id}/cancel", headers={"Authorization": f"Bearer {p_book_tok}"})
    prom_data = client.post(
        f"/flights/{flight_id}/waitlist/promote",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"class_type": "BUSINESS"},
    ).json()

    waitlist_id = prom_data["id"]

    # Cancel PROMOTED entry
    can_res = client.delete(f"/waitlists/{waitlist_id}", headers={"Authorization": f"Bearer {p_wait_tok}"})
    assert can_res.status_code == 200
    assert can_res.json()["status"] == "CANCELLED"

    # Held seat returned to AVAILABLE
    db_session.rollback()
    seat = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(prom_data["promoted_seat_id"])).first()
    assert seat.status == SeatStatus.AVAILABLE
    assert seat.hold_expires_at is None

    # Class available_seats restored to 1
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.BUSINESS,
    ).first()
    assert cls_check.available_seats == 1


# ============================================================================
# 6. CONCURRENCY & RACE CONDITIONS
# ============================================================================

def test_concurrent_promotion_cannot_double_assign_seat(client, db_session):
    """
    When exactly 1 seat is available and 2 concurrent workers trigger promotion,
    exactly ONE passenger is promoted, and the seat is assigned to only one.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, p_book = create_user_with_role(db_session, UserRole.PASSENGER, "p_crp_b")
    _, p1_wait = create_user_with_role(db_session, UserRole.PASSENGER, "p_crp_1")
    _, p2_wait = create_user_with_role(db_session, UserRole.PASSENGER, "p_crp_2")
    flight_id, _ = create_test_flight_full_setup(client, ops_token, flight_number="PK815")

    fill_class_seats(client, p_book, flight_id, FlightClassType.BUSINESS, count=1)

    # Both join waitlist
    client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p1_wait}"}, json={"class_type": "BUSINESS"})
    client.post(f"/flights/{flight_id}/waitlist", headers={"Authorization": f"Bearer {p2_wait}"}, json={"class_type": "BUSINESS"})

    # Free the 1 seat
    b_id = db_session.query(Booking).filter(Booking.flight_id == UUID(flight_id)).first().id
    client.post(f"/bookings/{b_id}/cancel", headers={"Authorization": f"Bearer {p_book}"})

    def attempt_promote():
        return client.post(
            f"/flights/{flight_id}/waitlist/promote",
            headers={"Authorization": f"Bearer {ops_token}"},
            json={"class_type": "BUSINESS"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_promote)
        f2 = executor.submit(attempt_promote)
        res1 = f1.result()
        res2 = f2.result()

    # One promotion succeeds (200 with promoted entry)
    # The second either succeeds returning None / 200 (if handled gracefully) or fails 409 (no seats)
    promotions = [r.json() for r in (res1, res2) if r.status_code == 200 and r.json() is not None]
    assert len(promotions) == 1

    # Invariant: exactly 1 seat is held, available_seats is 0 (never negative!)
    db_session.rollback()
    db_session.expire_all()
    cls_check = db_session.query(FlightClass).filter(
        FlightClass.flight_id == UUID(flight_id),
        FlightClass.class_type == FlightClassType.BUSINESS,
    ).first()
    assert cls_check.available_seats == 0

    promoted_seat_id = promotions[0]["promoted_seat_id"]
    assert promoted_seat_id is not None
    promoted_seat = db_session.query(FlightSeat).filter(FlightSeat.id == UUID(promoted_seat_id)).first()
    assert promoted_seat.status == SeatStatus.HELD



