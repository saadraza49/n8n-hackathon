from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Tuple
from uuid import uuid4

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
    FraudDecision,
    RefundReason,
    RefundStatus,
    RefundType,
    RiskLevel,
    SeatStatus,
    UserRole,
)
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.fraud import FraudEvaluation, RiskSignalRecord
from app.models.user import User


# ============================================================================
# TEST FIXTURES & HELPERS
# ============================================================================

def create_user_with_role(db_session: Session, role: UserRole, email_prefix: str = "user", hours_old: int = 48) -> Tuple[User, str]:
    """Create a user with a specific role, created_at offset, and generate a JWT Bearer token."""
    now_utc = datetime.now(timezone.utc)
    user = User(
        name=f"Test {role.value}",
        email=f"{email_prefix}_{uuid4().hex[:6]}@example.com",
        password_hash=hash_password("test_password123"),
        role=role,
        created_at=now_utc - timedelta(hours=hours_old),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(data={"sub": str(user.id), "email": user.email})
    return user, token


def create_test_flight(db_session: Session, hours_until_dep: int = 72) -> Flight:
    """Create a test flight with physical seats and classes."""
    now_utc = datetime.now(timezone.utc)
    dep_at = now_utc + timedelta(hours=hours_until_dep)
    arr_at = dep_at + timedelta(hours=4)

    flight = Flight(
        flight_number=f"PK{uuid4().hex[:3].upper()}",
        origin="ISB",
        destination="DXB",
        departure_at=dep_at,
        arrival_at=arr_at,
        status=FlightStatus.SCHEDULED,
        total_capacity=10,
    )
    db_session.add(flight)
    db_session.flush()

    fc = FlightClass(
        flight_id=flight.id,
        class_type=FlightClassType.ECONOMY,
        total_seats=10,
        available_seats=10,
    )
    db_session.add(fc)
    db_session.flush()

    for row in range(1, 6):
        for letter in ["A", "B"]:
            seat = FlightSeat(
                flight_id=flight.id,
                seat_number=f"{row}{letter}",
                class_type=FlightClassType.ECONOMY,
                status=SeatStatus.AVAILABLE,
            )
            db_session.add(seat)

    db_session.commit()
    db_session.refresh(flight)
    return flight


def create_test_booking(
    db_session: Session,
    user: User,
    flight: Flight,
    total_amount: Decimal = Decimal("250.00"),
    passenger_names: list = None,
    created_hours_ago: int = 0,
    booking_status: BookingStatus = BookingStatus.CONFIRMED,
) -> Booking:
    """Create a fully persisted booking with items and seats."""
    if passenger_names is None:
        passenger_names = ["Test Passenger"]

    now_utc = datetime.now(timezone.utc)
    booking = Booking(
        booking_reference=f"BK{uuid4().hex[:6].upper()}",
        user_id=user.id,
        flight_id=flight.id,
        status=booking_status,
        total_amount=total_amount,
        currency="USD",
        created_at=now_utc - timedelta(hours=created_hours_ago),
    )
    db_session.add(booking)
    db_session.flush()

    seats = (
        db_session.query(FlightSeat)
        .filter(FlightSeat.flight_id == flight.id, FlightSeat.status == SeatStatus.AVAILABLE)
        .limit(len(passenger_names))
        .all()
    )

    for i, name in enumerate(passenger_names):
        if i < len(seats):
            seat = seats[i]
        else:
            seat = FlightSeat(
                flight_id=flight.id,
                seat_number=f"{i+1}X",
                class_type=FlightClassType.ECONOMY,
                status=SeatStatus.AVAILABLE,
            )
            db_session.add(seat)
            db_session.flush()

        item = BookingItem(
            booking_id=booking.id,
            flight_id=flight.id,
            seat_id=seat.id,
            passenger_name=name,
            class_type=FlightClassType.ECONOMY,
            fare_type=FareType.BASIC,
            price=total_amount / Decimal(len(passenger_names)),
            currency="USD",
            status=BookingItemStatus.CONFIRMED,
        )
        db_session.add(item)

    db_session.commit()
    db_session.refresh(booking)
    return booking


# ============================================================================
# 1. RISK LEVEL EVALUATION TIERS (LOW, MEDIUM, HIGH, CRITICAL)
# ============================================================================

def test_fraud_evaluation_low_risk_case(client, db_session):
    """
    1. LOW risk evaluation (0-29):
    - Well in advance (>48h)
    - Low transaction value (<$1000)
    - Mature user account (>48h old)
    - Single passenger
    Expected: Score < 30, Risk Level: LOW, Decision: ALLOW
    """
    ops_user, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_low", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=96)
    booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("200.00"), passenger_names=["Alice Smith"])

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"source": "AGENT_TEST", "force_re_evaluate": True},
    )

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["booking_id"] == str(booking.id)
    assert data["booking_reference"] == booking.booking_reference
    assert data["risk_score"] < 30
    assert data["risk_level"] == RiskLevel.LOW.value
    assert data["decision"] == FraudDecision.ALLOW.value
    assert data["evaluator"] == "AGENT_TEST"
    assert "ALLOW" in data["reasons"]["summary"]

    # Verify audit log integration
    audit = db_session.query(AuditLog).filter(
        AuditLog.entity_id == booking.id,
        AuditLog.action == "FRAUD_EVALUATION",
    ).first()
    assert audit is not None
    assert audit.new_values["decision"] == FraudDecision.ALLOW.value
    assert audit.new_values["risk_level"] == RiskLevel.LOW.value


def test_fraud_evaluation_medium_risk_case(client, db_session):
    """
    2. MEDIUM risk evaluation (30-59):
    - Last-minute departure: flight departs in 18h (<24h: +15 pts)
    - High transaction value: $3,500 (>= $3,000: +25 pts)
    - Expected score: 15 + 25 = 40 pts
    Expected: 30 <= Score < 60, Risk Level: MEDIUM, Decision: REVIEW
    """
    ops_user, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_med", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=18)
    booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("3500.00"), passenger_names=["Bob Jones"])

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )

    assert res.status_code == 200, res.text
    data = res.json()
    assert 30 <= data["risk_score"] < 60
    assert data["risk_level"] == RiskLevel.MEDIUM.value
    assert data["decision"] == FraudDecision.REVIEW.value
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "LAST_MINUTE_DEPARTURE" in signal_codes
    assert "HIGH_TRANSACTION_VALUE" in signal_codes
    assert "REVIEW" in data["reasons"]["summary"]


def test_fraud_evaluation_high_risk_case(client, db_session):
    """
    3. HIGH risk evaluation (60-79):
    - Last-minute departure: flight departs in 2h (<6h: +25 pts)
    - High transaction value: $5,500 (>= $5,000: +35 pts)
    - Expected score: 25 + 35 = 60 pts
    Expected: 60 <= Score < 80, Risk Level: HIGH, Decision: REVIEW
    """
    ops_user, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_high", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=2)
    booking = create_test_booking(
        db_session,
        passenger,
        flight,
        total_amount=Decimal("5500.00"),
        passenger_names=["Agent Scully"],
    )

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )

    assert res.status_code == 200, res.text
    data = res.json()
    assert 60 <= data["risk_score"] < 80
    assert data["risk_level"] == RiskLevel.HIGH.value
    assert data["decision"] == FraudDecision.REVIEW.value
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "LAST_MINUTE_DEPARTURE" in signal_codes
    assert "HIGH_TRANSACTION_VALUE" in signal_codes
    assert "REVIEW" in data["reasons"]["summary"]


def test_fraud_evaluation_critical_risk_case(client, db_session):
    """
    4. CRITICAL risk evaluation (80-100):
    - Last-minute departure: flight departs in 3h (<6h: +25 pts)
    - High transaction value: $5,200 (>= $5,000: +35 pts)
    - Duplicate passenger names: ["Charlie Brown", "Charlie Brown"] (+30 pts)
    - Expected score: 25 + 35 + 30 = 90 pts
    Expected: Score >= 80, Risk Level: CRITICAL, Decision: BLOCK
    """
    ops_user, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_crit", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=3)
    booking = create_test_booking(
        db_session,
        passenger,
        flight,
        total_amount=Decimal("5200.00"),
        passenger_names=["Charlie Brown", "Charlie Brown"],
    )

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["risk_score"] >= 80
    assert data["risk_level"] == RiskLevel.CRITICAL.value
    assert data["decision"] == FraudDecision.BLOCK.value
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "LAST_MINUTE_DEPARTURE" in signal_codes
    assert "HIGH_TRANSACTION_VALUE" in signal_codes
    assert "DUPLICATE_PASSENGER_NAMES" in signal_codes
    assert "BLOCK" in data["reasons"]["summary"]


# ============================================================================
# 2. INDIVIDUAL FRAUD SIGNALS VERIFICATION
# ============================================================================

def test_fraud_evaluation_velocity_signal(client, db_session):
    """Verify RAPID_BOOKING_VELOCITY trigger when user creates >=3 bookings in 24 hours."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_vel", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=96)

    # Pre-create 3 existing bookings for this passenger within 24h
    for _ in range(3):
        create_test_booking(db_session, passenger, flight, total_amount=Decimal("100.00"), created_hours_ago=2)

    # Create the 4th booking to evaluate
    target_booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("150.00"), created_hours_ago=0)

    res = client.post(
        f"/bookings/{target_booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )
    assert res.status_code == 200
    data = res.json()
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "RAPID_BOOKING_VELOCITY" in signal_codes


def test_fraud_evaluation_refund_history_signal(client, db_session):
    """Verify FREQUENT_REFUND_ACTIVITY trigger when user has >=2 prior completed refunds."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_ref", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=96)

    # Pre-create 2 bookings with completed refunds
    for _ in range(2):
        old_b = create_test_booking(db_session, passenger, flight, total_amount=Decimal("200.00"), created_hours_ago=10)
        refund = Refund(
            booking_id=old_b.id,
            amount=Decimal("200.00"),
            currency="USD",
            refund_type=RefundType.MONETARY,
            status=RefundStatus.COMPLETED,
            reason=RefundReason.CUSTOMER_CANCELLATION,
        )
        db_session.add(refund)
    db_session.commit()

    # Create target booking to evaluate
    target_booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("250.00"))

    res = client.post(
        f"/bookings/{target_booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )
    assert res.status_code == 200
    data = res.json()
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "FREQUENT_REFUND_ACTIVITY" in signal_codes


def test_fraud_evaluation_new_account_high_exposure_signal(client, db_session):
    """Verify NEW_ACCOUNT_HIGH_EXPOSURE trigger when account is <24h old and booking >= $1000."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    # Brand new passenger (account created 2 hours ago)
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_new", hours_old=2)
    flight = create_test_flight(db_session, hours_until_dep=96)
    booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("1200.00"))

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )
    assert res.status_code == 200
    data = res.json()
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "NEW_ACCOUNT_HIGH_EXPOSURE" in signal_codes


def test_fraud_evaluation_large_party_size_signal(client, db_session):
    """Verify LARGE_PARTY_SIZE trigger when single booking has >=4 passengers."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_party", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=96)
    booking = create_test_booking(
        db_session,
        passenger,
        flight,
        total_amount=Decimal("800.00"),
        passenger_names=["Person 1", "Person 2", "Person 3", "Person 4"],
    )

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )
    assert res.status_code == 200
    data = res.json()
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "LARGE_PARTY_SIZE" in signal_codes


def test_fraud_evaluation_booking_changes_signal(client, db_session):
    """Verify FREQUENT_BOOKING_CHANGES signal when booking has >=2 prior modifications."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_changes")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_changes")
    flight = create_test_flight(db_session, hours_until_dep=96)
    booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("400.00"))

    seats = db_session.query(FlightSeat).filter(FlightSeat.flight_id == flight.id).limit(2).all()
    item = booking.items[0]

    # Add 2 booking changes
    for _ in range(2):
        bc = BookingChange(
            booking_id=booking.id,
            booking_item_id=item.id,
            old_flight_id=flight.id,
            new_flight_id=flight.id,
            old_seat_id=seats[0].id,
            new_seat_id=seats[1].id,
            old_class_type=FlightClassType.ECONOMY,
            new_class_type=FlightClassType.ECONOMY,
            old_fare_type=FareType.BASIC,
            new_fare_type=FareType.FLEXIBLE,
            old_price=Decimal("100.00"),
            new_price=Decimal("150.00"),
            price_difference=Decimal("50.00"),
            currency="USD",
        )
        db_session.add(bc)
    db_session.commit()

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )
    assert res.status_code == 200
    data = res.json()
    signal_codes = [s["code"] for s in data["reasons"]["signals"]]
    assert "FREQUENT_BOOKING_CHANGES" in signal_codes


# ============================================================================
# 3. IDEMPOTENCY, REPEATED EVALUATION & HISTORY PRESERVATION
# ============================================================================

def test_repeated_evaluation_and_history_retention(client, db_session):
    """
    Verify:
    1. force_re_evaluate=False returns existing evaluation without duplicate row.
    2. force_re_evaluate=True appends a new evaluation to history.
    3. GET /{booking_id}/fraud-evaluations returns all historical evaluations.
    4. GET /{booking_id}/fraud-evaluation/latest returns the most recent evaluation.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_repeat", hours_old=72)
    flight = create_test_flight(db_session, hours_until_dep=96)
    booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("300.00"))

    # Initial evaluation
    res1 = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"source": "AGENT_RUN_1", "force_re_evaluate": True},
    )
    assert res1.status_code == 200
    eval1_id = res1.json()["id"]

    # Re-evaluate with force_re_evaluate=False -> should return existing evaluation
    res_cached = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"source": "AGENT_RUN_CACHED", "force_re_evaluate": False},
    )
    assert res_cached.status_code == 200
    assert res_cached.json()["id"] == eval1_id

    # Count DB records: should still be 1
    eval_count = db_session.query(FraudEvaluation).filter(FraudEvaluation.booking_id == booking.id).count()
    assert eval_count == 1

    # Force re-evaluate -> should append second evaluation
    res2 = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"source": "AGENT_RUN_2", "force_re_evaluate": True},
    )
    assert res2.status_code == 200
    eval2_id = res2.json()["id"]
    assert eval2_id != eval1_id

    # Count DB records: should now be 2
    eval_count = db_session.query(FraudEvaluation).filter(FraudEvaluation.booking_id == booking.id).count()
    assert eval_count == 2

    # GET all evaluations
    res_list = client.get(
        f"/bookings/{booking.id}/fraud-evaluations",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total_evaluations"] == 2
    assert len(list_data["evaluations"]) == 2
    assert list_data["evaluations"][0]["id"] == eval2_id
    assert list_data["evaluations"][1]["id"] == eval1_id

    # GET latest evaluation
    res_latest = client.get(
        f"/bookings/{booking.id}/fraud-evaluation/latest",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_latest.status_code == 200
    assert res_latest.json()["id"] == eval2_id


def test_fraud_evaluation_idempotency_key_replay_and_conflict(client, db_session):
    """
    Verify:
    1. Replaying exact same idempotency_key returns cached evaluation.
    2. Reusing same idempotency_key for a DIFFERENT booking raises 409 Conflict.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_idem")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_idem")
    flight = create_test_flight(db_session)
    b1 = create_test_booking(db_session, passenger, flight, total_amount=Decimal("300.00"))
    b2 = create_test_booking(db_session, passenger, flight, total_amount=Decimal("400.00"))

    key = f"idem_fraud_{uuid4().hex[:12]}"

    # Initial call on b1 with Idempotency-Key
    res1 = client.post(
        f"/bookings/{b1.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}", "Idempotency-Key": key},
        json={"source": "N8N_WORKFLOW"},
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["idempotency_key"] == key
    eval_id1 = data1["id"]

    # Replay on b1 with exact same key -> must return cached evaluation
    res_replay = client.post(
        f"/bookings/{b1.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}", "Idempotency-Key": key},
    )
    assert res_replay.status_code == 200
    assert res_replay.json()["id"] == eval_id1

    # Conflict: Call on b2 with same key -> must return 409 Conflict
    res_conflict = client.post(
        f"/bookings/{b2.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}", "Idempotency-Key": key},
    )
    assert res_conflict.status_code == 409
    assert "Idempotency key reused with different booking" in res_conflict.json()["detail"]


# ============================================================================
# 4. RBAC & PERMISSION BOUNDARIES
# ============================================================================

def test_fraud_evaluation_rbac_enforcement(client, db_session):
    """
    Verify RBAC security:
    - Unauthenticated: 401 Unauthorized
    - PASSENGER role: 403 Forbidden
    - OPS_AGENT role: 200 OK
    - SUPER_ADMIN role: 200 OK
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_rbac")
    _, admin_token = create_user_with_role(db_session, UserRole.SUPER_ADMIN, "admin_rbac")
    passenger, passenger_token = create_user_with_role(db_session, UserRole.PASSENGER, "passenger_rbac")
    flight = create_test_flight(db_session, hours_until_dep=72)
    booking = create_test_booking(db_session, passenger, flight)

    # 1. Unauthenticated
    res_unauth = client.post(f"/bookings/{booking.id}/fraud-evaluation")
    assert res_unauth.status_code == 401

    # 2. Passenger role (forbidden)
    res_passenger = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {passenger_token}"},
    )
    assert res_passenger.status_code == 403

    res_passenger_list = client.get(
        f"/bookings/{booking.id}/fraud-evaluations",
        headers={"Authorization": f"Bearer {passenger_token}"},
    )
    assert res_passenger_list.status_code == 403

    # 3. Ops agent (allowed)
    res_ops = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_ops.status_code == 200

    # 4. Super admin (allowed)
    res_admin = client.get(
        f"/bookings/{booking.id}/fraud-evaluations",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_admin.status_code == 200


# ============================================================================
# 5. ERROR HANDLING, CANCELLATION & EDGE CASES
# ============================================================================

def test_fraud_evaluation_nonexistent_booking(client, db_session):
    """Verify 404 response when evaluating nonexistent booking ID."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_404")
    non_existent_id = uuid4()

    res = client.post(
        f"/bookings/{non_existent_id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res.status_code == 404
    assert f"Booking with ID '{non_existent_id}' not found" in res.json()["detail"]


def test_get_latest_fraud_evaluation_when_none_exists(client, db_session):
    """Verify 404 response when querying latest evaluation on booking with no evaluations."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_no_eval")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_no_eval")
    flight = create_test_flight(db_session)
    booking = create_test_booking(db_session, passenger, flight)

    res = client.get(
        f"/bookings/{booking.id}/fraud-evaluation/latest",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res.status_code == 404
    assert f"No fraud evaluations found for booking '{booking.id}'" in res.json()["detail"]


def test_fraud_evaluation_on_cancelled_booking(client, db_session):
    """Verify evaluating a cancelled booking evaluates correctly and preserves booking status."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_canc")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_canc")
    flight = create_test_flight(db_session)
    booking = create_test_booking(
        db_session, passenger, flight, total_amount=Decimal("300.00"), booking_status=BookingStatus.CANCELLED
    )

    res = client.post(
        f"/bookings/{booking.id}/fraud-evaluation",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"force_re_evaluate": True},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["booking_id"] == str(booking.id)
    # Check that booking status was not altered
    db_session.refresh(booking)
    assert booking.status == BookingStatus.CANCELLED


# ============================================================================
# 6. OPERATIONAL /fraud & /risk ROUTER ENDPOINTS & CHILD SIGNALS
# ============================================================================

def test_operational_fraud_endpoints_and_filtering(client, db_session):
    """
    Verify:
    1. POST /fraud/evaluate creates evaluation using payload booking_id.
    2. GET /fraud/evaluations lists evaluations with risk_level and decision filters.
    3. GET /fraud/evaluations/{id} returns specific evaluation details.
    4. GET /fraud/evaluations/{id}/signals returns child risk_signals records.
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_op")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_op")
    flight_high = create_test_flight(db_session, hours_until_dep=2)
    flight_low = create_test_flight(db_session, hours_until_dep=96)

    # Booking 1: High risk (last minute departure + high value + duplicate names = 25+35+30 = 90 pts -> CRITICAL / BLOCK)
    b_high = create_test_booking(
        db_session,
        passenger,
        flight_high,
        total_amount=Decimal("5500.00"),
        passenger_names=["Double Agent", "Double Agent"],
    )
    # Booking 2: Low risk
    b_low = create_test_booking(db_session, passenger, flight_low, total_amount=Decimal("150.00"))

    # 1. POST /fraud/evaluate on high risk booking
    res_eval = client.post(
        "/fraud/evaluate",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"booking_id": str(b_high.id), "source": "N8N_CRON_SCAN", "force_re_evaluate": True},
    )
    assert res_eval.status_code == 200
    eval_high_id = res_eval.json()["id"]
    assert res_eval.json()["risk_level"] == RiskLevel.CRITICAL.value
    assert res_eval.json()["decision"] == FraudDecision.BLOCK.value

    # Evaluate low risk booking
    client.post(
        "/fraud/evaluate",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"booking_id": str(b_low.id), "source": "N8N_CRON_SCAN", "force_re_evaluate": True},
    )

    # 2. Filter /fraud/evaluations by risk_level=CRITICAL
    res_list_high = client.get(
        "/fraud/evaluations?risk_level=CRITICAL",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_list_high.status_code == 200
    data_high = res_list_high.json()
    assert data_high["total"] >= 1
    assert all(item["risk_level"] == RiskLevel.CRITICAL.value for item in data_high["items"])

    # 3. Filter by decision=ALLOW
    res_list_allow = client.get(
        "/fraud/evaluations?decision=ALLOW",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_list_allow.status_code == 200
    data_allow = res_list_allow.json()
    assert all(item["decision"] == FraudDecision.ALLOW.value for item in data_allow["items"])

    # 4. GET /fraud/evaluations/{id}
    res_get_single = client.get(
        f"/fraud/evaluations/{eval_high_id}",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_get_single.status_code == 200
    assert res_get_single.json()["id"] == eval_high_id
    assert res_get_single.json()["evaluator"] == "N8N_CRON_SCAN"

    # 5. GET /fraud/evaluations/{id}/signals (child table verification)
    res_signals = client.get(
        f"/fraud/evaluations/{eval_high_id}/signals",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_signals.status_code == 200
    signals_list = res_signals.json()
    assert len(signals_list) >= 3
    sig_codes = [s["signal_code"] for s in signals_list]
    assert "LAST_MINUTE_DEPARTURE" in sig_codes
    assert "HIGH_TRANSACTION_VALUE" in sig_codes
    assert "DUPLICATE_PASSENGER_NAMES" in sig_codes


def test_risk_router_alias_endpoints(client, db_session):
    """
    Verify /risk endpoints parity:
    - POST /risk/evaluate/{booking_id}
    - GET /risk/evaluations
    - GET /risk/evaluations/{id}
    - GET /risk/evaluations/{id}/signals
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_risk_alias")
    passenger, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_risk_alias")
    flight = create_test_flight(db_session, hours_until_dep=96)
    booking = create_test_booking(db_session, passenger, flight, total_amount=Decimal("350.00"))

    # POST /risk/evaluate/{booking_id}
    res_eval = client.post(
        f"/risk/evaluate/{booking.id}",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"source": "RISK_API_ALIAS", "force_re_evaluate": True},
    )
    assert res_eval.status_code == 200
    eval_id = res_eval.json()["id"]

    # GET /risk/evaluations
    res_list = client.get(
        "/risk/evaluations",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_list.status_code == 200
    assert res_list.json()["total"] >= 1

    # GET /risk/evaluations/{id}
    res_single = client.get(
        f"/risk/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_single.status_code == 200
    assert res_single.json()["evaluator"] == "RISK_API_ALIAS"

    # GET /risk/evaluations/{id}/signals
    res_signals = client.get(
        f"/risk/evaluations/{eval_id}/signals",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_signals.status_code == 200
