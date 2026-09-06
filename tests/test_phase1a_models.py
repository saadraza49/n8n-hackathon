from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.audit_log import AuditLog
from app.models.enums import FareType, FlightClassType, FlightStatus, SeatStatus, UserRole
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User


# ==========================================
# 1. AUTH & ROLE TESTS
# ==========================================

def test_signup_assigns_default_passenger_role(client):
    """Verify that a normal user registration automatically assigns PASSENGER role."""
    response = client.post(
        "/auth/signup",
        json={
            "name": "Clark Kent",
            "email": "clark@dailyplanet.com",
            "password": "superman_secure_password",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["role"] == "PASSENGER"
    assert "password_hash" not in data


def test_signup_prevents_privilege_escalation(client):
    """Verify that attempting to inject OPS_AGENT or SUPER_ADMIN in signup is ignored/blocked."""
    response = client.post(
        "/auth/signup",
        json={
            "name": "Lex Luthor",
            "email": "lex@lexcorp.com",
            "password": "lex_secure_password",
            "role": "SUPER_ADMIN",
        },
    )
    assert response.status_code == 201
    data = response.json()
    # Role must still be PASSENGER, privilege escalation must be prevented
    assert data["role"] == "PASSENGER"


def test_auth_me_returns_user_role(client):
    """Verify /auth/me returns the user's role."""
    # 1. Signup
    client.post(
        "/auth/signup",
        json={
            "name": "Diana Prince",
            "email": "diana@themyscira.com",
            "password": "wonder_woman_password",
        },
    )
    # 2. Login
    login_res = client.post(
        "/auth/login",
        json={
            "email": "diana@themyscira.com",
            "password": "wonder_woman_password",
        },
    )
    token = login_res.json()["access_token"]

    # 3. /auth/me
    me_res = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    assert me_res.json()["role"] == "PASSENGER"


# ==========================================
# 2. FLIGHT CONSTRAINTS
# ==========================================

def test_flight_valid_creation(db_session):
    """Verify a valid flight can be created."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="PK786",
        origin="ISB",
        destination="LHR",
        departure_at=now + timedelta(days=1),
        arrival_at=now + timedelta(days=1, hours=8),
        total_capacity=300,
        status=FlightStatus.SCHEDULED,
    )
    db_session.add(flight)
    db_session.commit()
    db_session.refresh(flight)
    assert flight.id is not None
    assert flight.status == FlightStatus.SCHEDULED


def test_flight_total_capacity_positive_constraint(db_session):
    """CheckConstraint ck_flights_total_capacity_positive: total_capacity > 0."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="PK786",
        origin="ISB",
        destination="LHR",
        departure_at=now + timedelta(days=1),
        arrival_at=now + timedelta(days=1, hours=8),
        total_capacity=0,  # Invalid
    )
    db_session.add(flight)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_flight_arrival_after_departure_constraint(db_session):
    """CheckConstraint ck_flights_arrival_after_departure: arrival_at > departure_at."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="PK786",
        origin="ISB",
        destination="LHR",
        departure_at=now + timedelta(days=1, hours=8),
        arrival_at=now + timedelta(days=1),  # Invalid: arrival before departure
        total_capacity=200,
    )
    db_session.add(flight)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_flight_origin_ne_destination_constraint(db_session):
    """CheckConstraint ck_flights_origin_ne_destination: origin != destination."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="PK786",
        origin="DXB",
        destination="DXB",  # Invalid: same origin and destination
        departure_at=now + timedelta(days=1),
        arrival_at=now + timedelta(days=1, hours=3),
        total_capacity=150,
    )
    db_session.add(flight)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ==========================================
# 3. FLIGHT CLASSES CONSTRAINTS
# ==========================================

def test_flight_class_unique_per_flight(db_session):
    """UniqueConstraint: uq_flight_classes_flight_class (flight_id, class_type)."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="QR101",
        origin="DOH",
        destination="JFK",
        departure_at=now + timedelta(days=2),
        arrival_at=now + timedelta(days=2, hours=14),
        total_capacity=300,
    )
    db_session.add(flight)
    db_session.commit()

    # Add first ECONOMY class
    fc1 = FlightClass(
        flight_id=flight.id,
        class_type=FlightClassType.ECONOMY,
        total_seats=200,
        available_seats=200,
    )
    db_session.add(fc1)
    db_session.commit()

    # Add duplicate ECONOMY class for the same flight
    fc2 = FlightClass(
        flight_id=flight.id,
        class_type=FlightClassType.ECONOMY,
        total_seats=50,
        available_seats=50,
    )
    db_session.add(fc2)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_flight_class_seats_constraints(db_session):
    """CheckConstraints on flight_classes: total_seats > 0, available_seats >= 0, available_seats <= total_seats."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="BA202",
        origin="LHR",
        destination="DXB",
        departure_at=now + timedelta(days=1),
        arrival_at=now + timedelta(days=1, hours=7),
        total_capacity=200,
    )
    db_session.add(flight)
    db_session.commit()

    # Invalid: available_seats > total_seats
    fc_invalid = FlightClass(
        flight_id=flight.id,
        class_type=FlightClassType.BUSINESS,
        total_seats=30,
        available_seats=35,
    )
    db_session.add(fc_invalid)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Invalid: available_seats < 0
    fc_negative = FlightClass(
        flight_id=flight.id,
        class_type=FlightClassType.BUSINESS,
        total_seats=30,
        available_seats=-1,
    )
    db_session.add(fc_negative)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Invalid: total_seats = 0
    fc_zero = FlightClass(
        flight_id=flight.id,
        class_type=FlightClassType.BUSINESS,
        total_seats=0,
        available_seats=0,
    )
    db_session.add(fc_zero)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ==========================================
# 4. FLIGHT SEATS CONSTRAINTS
# ==========================================

def test_flight_seat_unique_seat_number_per_flight(db_session):
    """UniqueConstraint: uq_flight_seats_flight_seat (flight_id, seat_number)."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="EK501",
        origin="KHI",
        destination="DXB",
        departure_at=now + timedelta(days=1),
        arrival_at=now + timedelta(days=1, hours=2),
        total_capacity=180,
    )
    db_session.add(flight)
    db_session.commit()

    seat1 = FlightSeat(
        flight_id=flight.id,
        seat_number="12A",
        class_type=FlightClassType.ECONOMY,
        status=SeatStatus.AVAILABLE,
    )
    db_session.add(seat1)
    db_session.commit()

    # Duplicate seat number on same flight
    seat2 = FlightSeat(
        flight_id=flight.id,
        seat_number="12A",
        class_type=FlightClassType.ECONOMY,
        status=SeatStatus.AVAILABLE,
    )
    db_session.add(seat2)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ==========================================
# 5. FARE RULES CONSTRAINTS
# ==========================================

def test_fare_rule_creation_and_uniqueness(db_session):
    """UniqueConstraint: uq_fare_rules_flight_class_fare (flight_id, class_type, fare_type)."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="EK502",
        origin="DXB",
        destination="KHI",
        departure_at=now + timedelta(days=3),
        arrival_at=now + timedelta(days=3, hours=2),
        total_capacity=180,
    )
    db_session.add(flight)
    db_session.commit()

    rule1 = FareRule(
        flight_id=flight.id,
        class_type=FlightClassType.ECONOMY,
        fare_type=FareType.BASIC,
        price=Decimal("250.00"),
        currency="USD",
        changes_allowed=False,
        seat_selection_allowed=False,
        refundable=False,
        credit_only=False,
        cancellation_cutoff_minutes=1440,
    )
    db_session.add(rule1)
    db_session.commit()

    # Duplicate rule for same flight/class/fare
    rule2 = FareRule(
        flight_id=flight.id,
        class_type=FlightClassType.ECONOMY,
        fare_type=FareType.BASIC,
        price=Decimal("280.00"),
        currency="USD",
    )
    db_session.add(rule2)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_fare_rule_price_non_negative_constraint(db_session):
    """CheckConstraint: price >= 0."""
    now = datetime.now(timezone.utc)
    flight = Flight(
        flight_number="EK503",
        origin="DXB",
        destination="KHI",
        departure_at=now + timedelta(days=3),
        arrival_at=now + timedelta(days=3, hours=2),
        total_capacity=180,
    )
    db_session.add(flight)
    db_session.commit()

    rule_negative = FareRule(
        flight_id=flight.id,
        class_type=FlightClassType.ECONOMY,
        fare_type=FareType.FLEXIBLE,
        price=Decimal("-50.00"),
        currency="USD",
    )
    db_session.add(rule_negative)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ==========================================
# 6. AUDIT LOGS & SET NULL BEHAVIOR
# ==========================================

def test_audit_log_set_null_on_user_delete(db_session):
    """Verify that deleting a user sets audit_log.user_id to NULL without deleting audit record."""
    # Create user
    user = User(
        name="Admin User",
        email="admin@system.com",
        password_hash="fakehash",
        role=UserRole.SUPER_ADMIN,
    )
    db_session.add(user)
    db_session.commit()

    # Create audit log
    audit = AuditLog(
        user_id=user.id,
        action="CREATE_FLIGHT",
        entity_type="FLIGHT",
        entity_id=uuid.uuid4(),
        old_values=None,
        new_values={"flight_number": "UK999"},
    )
    db_session.add(audit)
    db_session.commit()
    db_session.refresh(audit)
    audit_id = audit.id

    # Delete user
    db_session.delete(user)
    db_session.commit()

    # Verify audit log still exists, but user_id is now None
    reloaded_audit = db_session.query(AuditLog).filter(AuditLog.id == audit_id).first()
    assert reloaded_audit is not None
    assert reloaded_audit.user_id is None
    assert reloaded_audit.action == "CREATE_FLIGHT"
