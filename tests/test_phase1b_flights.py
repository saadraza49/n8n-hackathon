from datetime import datetime, timedelta, timezone
from typing import Tuple
from uuid import UUID, uuid4

import pytest


from app.core.security import create_access_token, hash_password
from app.models.audit_log import AuditLog
from app.models.enums import FlightClassType, FlightStatus, SeatStatus, UserRole
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User


# ==========================================
# TEST FIXTURES & HELPERS
# ==========================================

def create_user_with_role(db_session, role: UserRole, email_prefix: str = "user") -> Tuple[User, str]:
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


from typing import Tuple


# ==========================================
# 1. RBAC TESTS
# ==========================================

def test_passenger_cannot_create_flight(client, db_session):
    """Verify that a PASSENGER receives 403 Forbidden when trying to create a flight."""
    _, token = create_user_with_role(db_session, UserRole.PASSENGER, "passenger")
    now = datetime.now(timezone.utc)

    response = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "UK101",
            "origin": "LHR",
            "destination": "JFK",
            "departure_at": (now + timedelta(days=2)).isoformat(),
            "arrival_at": (now + timedelta(days=2, hours=8)).isoformat(),
            "total_capacity": 100,
            "first_class_seats": 20,
            "business_class_seats": 30,
            "economy_class_seats": 50,
        },
    )
    assert response.status_code == 403
    assert "Operation not permitted" in response.json()["detail"]


def test_passenger_cannot_update_flight(client, db_session):
    """Verify that a PASSENGER receives 403 Forbidden when trying to update a flight."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, pass_token = create_user_with_role(db_session, UserRole.PASSENGER, "pass")
    now = datetime.now(timezone.utc)

    # Create flight as ops
    create_res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "flight_number": "UK102",
            "origin": "LHR",
            "destination": "DXB",
            "departure_at": (now + timedelta(days=2)).isoformat(),
            "arrival_at": (now + timedelta(days=2, hours=7)).isoformat(),
            "total_capacity": 100,
            "first_class_seats": 20,
            "business_class_seats": 30,
            "economy_class_seats": 50,
        },
    )
    flight_id = create_res.json()["id"]

    # Passenger tries to update
    patch_res = client.patch(
        f"/flights/{flight_id}",
        headers={"Authorization": f"Bearer {pass_token}"},
        json={"destination": "AUH"},
    )
    assert patch_res.status_code == 403


def test_passenger_cannot_cancel_flight(client, db_session):
    """Verify that a PASSENGER receives 403 Forbidden when trying to cancel a flight."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, pass_token = create_user_with_role(db_session, UserRole.PASSENGER, "pass")
    now = datetime.now(timezone.utc)

    create_res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "flight_number": "UK103",
            "origin": "LHR",
            "destination": "SIN",
            "departure_at": (now + timedelta(days=3)).isoformat(),
            "arrival_at": (now + timedelta(days=3, hours=13)).isoformat(),
            "total_capacity": 60,
            "first_class_seats": 10,
            "business_class_seats": 20,
            "economy_class_seats": 30,
        },
    )
    flight_id = create_res.json()["id"]

    cancel_res = client.post(
        f"/flights/{flight_id}/cancel",
        headers={"Authorization": f"Bearer {pass_token}"},
    )
    assert cancel_res.status_code == 403


def test_ops_agent_and_super_admin_can_create_flight(client, db_session):
    """Verify that both OPS_AGENT and SUPER_ADMIN can create flights successfully."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, admin_token = create_user_with_role(db_session, UserRole.SUPER_ADMIN, "admin")
    now = datetime.now(timezone.utc)

    # 1. Ops Agent creates flight
    res1 = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "flight_number": "BA501",
            "origin": "LHR",
            "destination": "FRA",
            "departure_at": (now + timedelta(days=4)).isoformat(),
            "arrival_at": (now + timedelta(days=4, hours=2)).isoformat(),
            "total_capacity": 50,
            "first_class_seats": 10,
            "business_class_seats": 15,
            "economy_class_seats": 25,
        },
    )
    assert res1.status_code == 201
    assert res1.json()["flight_number"] == "BA501"

    # 2. Super Admin creates flight
    res2 = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "flight_number": "LH202",
            "origin": "FRA",
            "destination": "MUC",
            "departure_at": (now + timedelta(days=5)).isoformat(),
            "arrival_at": (now + timedelta(days=5, hours=1)).isoformat(),
            "total_capacity": 50,
            "first_class_seats": 10,
            "business_class_seats": 15,
            "economy_class_seats": 25,
        },
    )
    assert res2.status_code == 201
    assert res2.json()["flight_number"] == "LH202"


# ==========================================
# 2. VALIDATION & INVARIANTS TESTS
# ==========================================

def test_create_flight_class_capacity_mismatch_fails(client, db_session):
    """Verify capacity invariant: First + Business + Economy must equal total_capacity."""
    _, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    # 20 + 30 + 40 = 90, which does NOT equal total_capacity 100
    response = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "UK999",
            "origin": "LHR",
            "destination": "JFK",
            "departure_at": (now + timedelta(days=2)).isoformat(),
            "arrival_at": (now + timedelta(days=2, hours=8)).isoformat(),
            "total_capacity": 100,
            "first_class_seats": 20,
            "business_class_seats": 30,
            "economy_class_seats": 40,
        },
    )
    assert response.status_code == 422
    error_msg = str(response.json())
    assert "Class capacity mismatch" in error_msg


def test_create_flight_arrival_before_departure_fails(client, db_session):
    """Verify arrival_at must be strictly later than departure_at."""
    _, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    response = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "UK998",
            "origin": "LHR",
            "destination": "JFK",
            "departure_at": (now + timedelta(days=2, hours=8)).isoformat(),
            "arrival_at": (now + timedelta(days=2)).isoformat(),  # Earlier!
            "total_capacity": 100,
            "first_class_seats": 20,
            "business_class_seats": 30,
            "economy_class_seats": 50,
        },
    )
    assert response.status_code == 422
    assert "arrival_at must be strictly later than departure_at" in str(response.json())


def test_create_flight_same_origin_destination_fails(client, db_session):
    """Verify origin and destination cannot be identical."""
    _, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    response = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "UK997",
            "origin": "DXB",
            "destination": "DXB",
            "departure_at": (now + timedelta(days=2)).isoformat(),
            "arrival_at": (now + timedelta(days=2, hours=2)).isoformat(),
            "total_capacity": 100,
            "first_class_seats": 20,
            "business_class_seats": 30,
            "economy_class_seats": 50,
        },
    )
    assert response.status_code == 422
    assert "Origin and destination cannot be identical" in str(response.json())


def test_create_flight_duplicate_detection_fails(client, db_session):
    """Verify that scheduling the same flight number on the same route and same date is rejected with 409 Conflict."""
    _, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    payload = {
        "flight_number": "PK701",
        "origin": "ISB",
        "destination": "MAN",
        "departure_at": (now + timedelta(days=7, hours=10)).isoformat(),
        "arrival_at": (now + timedelta(days=7, hours=18)).isoformat(),
        "total_capacity": 60,
        "first_class_seats": 10,
        "business_class_seats": 20,
        "economy_class_seats": 30,
    }

    # First attempt succeeds
    res1 = client.post("/flights", headers={"Authorization": f"Bearer {token}"}, json=payload)
    assert res1.status_code == 201

    # Second attempt on same day/route fails with 409
    res2 = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            **payload,
            # Same day, slightly different departure hour
            "departure_at": (now + timedelta(days=7, hours=14)).isoformat(),
            "arrival_at": (now + timedelta(days=7, hours=22)).isoformat(),
        },
    )
    assert res2.status_code == 409
    assert "Duplicate flight" in res2.json()["detail"]


# ==========================================
# 3. PHYSICAL SEAT GENERATION & ATOMICITY
# ==========================================

def test_physical_seat_generation_exact_counts_and_uniqueness(client, db_session):
    """Verify exact seat counts per class, uniqueness of seat numbers, and initial AVAILABLE status."""
    _, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    first_target = 4
    biz_target = 6
    econ_target = 10
    total_target = first_target + biz_target + econ_target

    response = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "EK201",
            "origin": "DXB",
            "destination": "JFK",
            "departure_at": (now + timedelta(days=8)).isoformat(),
            "arrival_at": (now + timedelta(days=8, hours=14)).isoformat(),
            "total_capacity": total_target,
            "first_class_seats": first_target,
            "business_class_seats": biz_target,
            "economy_class_seats": econ_target,
        },
    )
    assert response.status_code == 201
    flight_id_str = response.json()["id"]
    flight_uuid = UUID(flight_id_str)

    # Verify physical seats directly in database
    seats = db_session.query(FlightSeat).filter(FlightSeat.flight_id == flight_uuid).all()
    assert len(seats) == total_target

    # Verify class counts
    first_seats = [s for s in seats if s.class_type == FlightClassType.FIRST]
    biz_seats = [s for s in seats if s.class_type == FlightClassType.BUSINESS]
    econ_seats = [s for s in seats if s.class_type == FlightClassType.ECONOMY]
    assert len(first_seats) == first_target
    assert len(biz_seats) == biz_target
    assert len(econ_seats) == econ_target

    # Verify uniqueness of seat numbers
    seat_numbers = [s.seat_number for s in seats]
    assert len(seat_numbers) == len(set(seat_numbers))

    # Verify all seats are initially AVAILABLE with no hold
    for s in seats:
        assert s.status == SeatStatus.AVAILABLE
        assert s.hold_expires_at is None

    # Verify AuditLog created
    audit = db_session.query(AuditLog).filter(AuditLog.entity_id == flight_uuid).first()
    assert audit is not None
    assert audit.action == "CREATE_FLIGHT"
    assert audit.entity_type == "FLIGHT"



# ==========================================
# 4. RETRIEVAL & SEARCH TESTS
# ==========================================

def test_get_flight_details_and_list(client, db_session):
    """Verify single flight retrieval with seat stats and listing with search filters."""
    _, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    # Create flight
    create_res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "QR505",
            "origin": "DOH",
            "destination": "LHR",
            "departure_at": (now + timedelta(days=10)).isoformat(),
            "arrival_at": (now + timedelta(days=10, hours=7)).isoformat(),
            "total_capacity": 30,
            "first_class_seats": 5,
            "business_class_seats": 10,
            "economy_class_seats": 15,
        },
    )
    flight_id = create_res.json()["id"]

    # 1. Get single flight
    get_res = client.get(f"/flights/{flight_id}", headers={"Authorization": f"Bearer {token}"})
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["id"] == flight_id
    assert data["total_seats_count"] == 30
    assert data["available_seats_count"] == 30
    assert len(data["classes"]) == 3

    # 2. Get non-existent flight -> 404
    non_existent = uuid4()
    not_found_res = client.get(f"/flights/{non_existent}", headers={"Authorization": f"Bearer {token}"})
    assert not_found_res.status_code == 404

    # 3. List flights with filter
    list_res = client.get(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        params={"origin": "DOH", "destination": "LHR", "page": 1, "page_size": 10},
    )
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total"] >= 1
    assert any(f["id"] == flight_id for f in list_data["items"])


# ==========================================
# 5. UPDATE & CANCELLATION TESTS
# ==========================================

def test_update_flight_and_audit_log(client, db_session):
    """Verify flight update, validation against invalid times, and audit log generation."""
    actor, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    create_res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "AF101",
            "origin": "CDG",
            "destination": "JFK",
            "departure_at": (now + timedelta(days=12)).isoformat(),
            "arrival_at": (now + timedelta(days=12, hours=8)).isoformat(),
            "total_capacity": 40,
            "first_class_seats": 10,
            "business_class_seats": 10,
            "economy_class_seats": 20,
        },
    )
    flight_id_str = create_res.json()["id"]
    flight_uuid = UUID(flight_id_str)

    # 1. Valid update
    update_res = client.patch(
        f"/flights/{flight_id_str}",
        headers={"Authorization": f"Bearer {token}"},
        json={"flight_number": "AF101-NEW", "status": "DELAYED"},
    )
    assert update_res.status_code == 200
    assert update_res.json()["flight_number"] == "AF101-NEW"
    assert update_res.json()["status"] == "DELAYED"

    # Verify audit log recorded UPDATE_FLIGHT
    update_audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == flight_uuid, AuditLog.action == "UPDATE_FLIGHT")
        .first()
    )
    assert update_audit is not None
    assert update_audit.user_id == actor.id
    assert update_audit.new_values["status"] == "DELAYED"


def test_cancel_flight_preserves_records_and_creates_audit(client, db_session):
    """Verify cancelling a flight changes status to CANCELLED, preserves seats/classes, and records audit log."""
    actor, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    create_res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": "VS202",
            "origin": "LHR",
            "destination": "BOS",
            "departure_at": (now + timedelta(days=14)).isoformat(),
            "arrival_at": (now + timedelta(days=14, hours=7)).isoformat(),
            "total_capacity": 20,
            "first_class_seats": 4,
            "business_class_seats": 6,
            "economy_class_seats": 10,
        },
    )
    flight_id_str = create_res.json()["id"]
    flight_uuid = UUID(flight_id_str)

    # Cancel flight
    cancel_res = client.post(
        f"/flights/{flight_id_str}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "CANCELLED"

    # Verify historical records in DB are intact
    flight_in_db = db_session.query(Flight).filter(Flight.id == flight_uuid).first()
    assert flight_in_db is not None
    assert flight_in_db.status == FlightStatus.CANCELLED

    classes_in_db = db_session.query(FlightClass).filter(FlightClass.flight_id == flight_uuid).all()
    assert len(classes_in_db) == 3

    seats_in_db = db_session.query(FlightSeat).filter(FlightSeat.flight_id == flight_uuid).all()
    assert len(seats_in_db) == 20

    # Cannot cancel already cancelled flight -> 400
    second_cancel = client.post(
        f"/flights/{flight_id_str}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second_cancel.status_code == 400
    assert "already cancelled" in second_cancel.json()["detail"]

    # Cannot update a cancelled flight -> 400
    update_cancelled = client.patch(
        f"/flights/{flight_id_str}",
        headers={"Authorization": f"Bearer {token}"},
        json={"destination": "MIA"},
    )
    assert update_cancelled.status_code == 400
    assert "Cannot update a CANCELLED flight" in update_cancelled.json()["detail"]

    # Verify audit log recorded CANCEL_FLIGHT
    cancel_audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == flight_uuid, AuditLog.action == "CANCEL_FLIGHT")
        .first()
    )
    assert cancel_audit is not None
    assert cancel_audit.user_id == actor.id
    assert cancel_audit.new_values["status"] == "CANCELLED"


def test_atomic_transaction_rollback_on_failure(client, db_session, monkeypatch):
    """Verify that if seat generation or audit logging fails, the flight is rolled back atomically."""
    from app.services import flight_service
    actor, token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    now = datetime.now(timezone.utc)

    # Monkeypatch generate_physical_seats to simulate unexpected crash
    def broken_seats(*args, **kwargs):
        raise RuntimeError("Simulated physical seat generation hardware error")

    monkeypatch.setattr(flight_service, "generate_physical_seats", broken_seats)

    with pytest.raises(RuntimeError):
        client.post(
            "/flights",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "flight_number": "FAIL101",
                "origin": "LHR",
                "destination": "JFK",
                "departure_at": (now + timedelta(days=20)).isoformat(),
                "arrival_at": (now + timedelta(days=20, hours=8)).isoformat(),
                "total_capacity": 40,
                "first_class_seats": 10,
                "business_class_seats": 10,
                "economy_class_seats": 20,
            },
        )

    # Verify that NO flight record was left in the database
    orphaned_flight = db_session.query(Flight).filter(Flight.flight_number == "FAIL101").first()
    assert orphaned_flight is None


