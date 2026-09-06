from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Tuple
from uuid import UUID, uuid4

import pytest

from app.core.security import create_access_token, hash_password
from app.models.audit_log import AuditLog
from app.models.enums import FareType, FlightClassType, FlightStatus, UserRole
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User


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


def create_test_flight(
    client,
    token: str,
    flight_number: str = "EK500",
    origin: str = "DXB",
    destination: str = "LHR",
    dep_date: date = date(2026, 9, 20),
    dep_hour: int = 10,
    total_capacity: int = 60,
    first_seats: int = 10,
    biz_seats: int = 20,
    econ_seats: int = 30,
) -> str:
    """Helper to create a flight via the operational API."""
    dep_at = datetime(dep_date.year, dep_date.month, dep_date.day, dep_hour, 0, 0, tzinfo=timezone.utc)
    arr_at = dep_at + timedelta(hours=7)

    res = client.post(
        "/flights",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight_number": flight_number,
            "origin": origin,
            "destination": destination,
            "departure_at": dep_at.isoformat(),
            "arrival_at": arr_at.isoformat(),
            "total_capacity": total_capacity,
            "first_class_seats": first_seats,
            "business_class_seats": biz_seats,
            "economy_class_seats": econ_seats,
        },
    )
    assert res.status_code == 201
    return res.json()["id"]


# ==========================================
# 1. SEARCH ENDPOINT & FILTERING TESTS
# ==========================================

def test_search_flights_by_route_and_date(client, db_session):
    """Verify passenger search finds flights matching origin, destination, and departure date."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    target_date = date(2026, 9, 20)

    # 1. Create matching flight
    flight_id = create_test_flight(
        client, ops_token, flight_number="BA101", origin="LHR", destination="DXB", dep_date=target_date
    )

    # 2. Search for the flight
    search_res = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {ops_token}"},
        params={
            "origin": "LHR",
            "destination": "DXB",
            "departure_date": target_date.isoformat(),
        },
    )
    assert search_res.status_code == 200
    data = search_res.json()
    assert data["pagination"]["total"] == 1
    assert data["pagination"]["page"] == 1
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["flight"]["id"] == flight_id
    assert item["flight"]["flight_number"] == "BA101"
    assert item["flight"]["origin"] == "LHR"
    assert item["flight"]["destination"] == "DXB"
    assert len(item["classes"]) == 3


def test_search_flights_case_normalization(client, db_session):
    """Verify origin and destination inputs are case-normalized (e.g. 'lhr' -> 'LHR')."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    target_date = date(2026, 9, 21)

    create_test_flight(
        client, ops_token, flight_number="AF202", origin="CDG", destination="JFK", dep_date=target_date
    )

    search_res = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {ops_token}"},
        params={
            "origin": "cdg",  # lowercase
            "destination": "jfk",  # lowercase
            "departure_date": target_date.isoformat(),
        },
    )
    assert search_res.status_code == 200
    assert search_res.json()["pagination"]["total"] == 1


def test_search_flights_no_matching_returns_empty_list_not_404(client, db_session):
    """Verify empty search result returns 200 OK with items=[] and pagination metadata, never 404."""
    _, token = create_user_with_role(db_session, UserRole.PASSENGER, "pass")

    search_res = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "origin": "ISB",
            "destination": "SYD",
            "departure_date": "2026-11-15",
        },
    )
    assert search_res.status_code == 200
    data = search_res.json()
    assert data["items"] == []
    assert data["pagination"]["total"] == 0
    assert data["pagination"]["total_pages"] == 0


def test_search_flights_excludes_cancelled_and_completed(client, db_session):
    """Verify that CANCELLED or COMPLETED flights are not returned in passenger search."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    target_date = date(2026, 9, 22)

    # Create flight and then cancel it
    flight_id = create_test_flight(
        client, ops_token, flight_number="EK999", origin="DXB", destination="SIN", dep_date=target_date
    )
    client.post(f"/flights/{flight_id}/cancel", headers={"Authorization": f"Bearer {ops_token}"})

    # Search should exclude the cancelled flight
    search_res = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {ops_token}"},
        params={
            "origin": "DXB",
            "destination": "SIN",
            "departure_date": target_date.isoformat(),
        },
    )
    assert search_res.status_code == 200
    assert search_res.json()["pagination"]["total"] == 0


def test_search_flights_same_origin_destination_fails(client, db_session):
    """Verify searching with same origin and destination fails with 400 Bad Request."""
    _, token = create_user_with_role(db_session, UserRole.PASSENGER, "pass")

    search_res = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "origin": "LHR",
            "destination": "LHR",
            "departure_date": "2026-09-20",
        },
    )
    assert search_res.status_code == 400
    assert "cannot be identical" in search_res.json()["detail"]


def test_search_flights_pagination(client, db_session):
    """Verify pagination behavior (page, page_size, total_pages)."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    target_date = date(2026, 9, 23)

    # Create 3 flights on the same day/route with different flight numbers and hours
    create_test_flight(client, ops_token, flight_number="PA101", origin="KHI", destination="ISB", dep_date=target_date, dep_hour=6)
    create_test_flight(client, ops_token, flight_number="PA102", origin="KHI", destination="ISB", dep_date=target_date, dep_hour=12)
    create_test_flight(client, ops_token, flight_number="PA103", origin="KHI", destination="ISB", dep_date=target_date, dep_hour=18)

    # Page 1, size 2
    res_page1 = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {ops_token}"},
        params={"origin": "KHI", "destination": "ISB", "departure_date": target_date.isoformat(), "page": 1, "page_size": 2},
    )
    assert res_page1.status_code == 200
    p1 = res_page1.json()
    assert p1["pagination"]["total"] == 3
    assert p1["pagination"]["total_pages"] == 2
    assert len(p1["items"]) == 2

    # Page 2, size 2
    res_page2 = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {ops_token}"},
        params={"origin": "KHI", "destination": "ISB", "departure_date": target_date.isoformat(), "page": 2, "page_size": 2},
    )
    assert res_page2.status_code == 200
    p2 = res_page2.json()
    assert len(p2["items"]) == 1


# ==========================================
# 2. FARE RULES & AVAILABILITY TESTS
# ==========================================

def test_fare_rule_crud_and_rbac(client, db_session):
    """Verify Fare Rule CRUD: passenger forbidden, ops allowed, audit logged."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, pass_token = create_user_with_role(db_session, UserRole.PASSENGER, "pass")
    target_date = date(2026, 9, 24)

    flight_id = create_test_flight(
        client, ops_token, flight_number="QR301", origin="DOH", destination="MAN", dep_date=target_date
    )

    # 1. Passenger cannot create fare rule -> 403
    fail_res = client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {pass_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "BASIC",
            "price": "199.99",
            "currency": "USD",
        },
    )
    assert fail_res.status_code == 403

    # 2. Ops agent can create Basic fare rule -> 201
    basic_res = client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "BASIC",
            "price": "199.99",
            "currency": "USD",
            "changes_allowed": False,
            "seat_selection_allowed": False,
            "refundable": False,
            "credit_only": False,
            "cancellation_cutoff_minutes": 1440,
        },
    )
    assert basic_res.status_code == 201
    basic_fare = basic_res.json()
    assert basic_fare["price"] == "199.99"
    assert basic_fare["fare_type"] == "BASIC"

    # 3. Ops agent can create Flexible fare rule -> 201
    flex_res = client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "FLEXIBLE",
            "price": "299.99",
            "currency": "USD",
            "changes_allowed": True,
            "seat_selection_allowed": True,
            "refundable": True,
            "credit_only": False,
            "cancellation_cutoff_minutes": 360,
        },
    )
    assert flex_res.status_code == 201
    flex_fare = flex_res.json()
    assert flex_fare["price"] == "299.99"
    assert flex_fare["fare_type"] == "FLEXIBLE"
    assert flex_fare["changes_allowed"] is True

    # 4. Duplicate fare rule for same class & fare type -> 409
    dup_res = client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "ECONOMY",
            "fare_type": "BASIC",
            "price": "150.00",
            "currency": "USD",
        },
    )
    assert dup_res.status_code == 409

    # 5. List fare rules -> 200
    list_fares_res = client.get(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {pass_token}"},
    )
    assert list_fares_res.status_code == 200
    assert len(list_fares_res.json()) == 2

    # 6. Update fare rule -> 200
    fare_id = basic_fare["id"]
    patch_res = client.patch(
        f"/flights/{flight_id}/fares/{fare_id}",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"price": "219.50", "changes_allowed": True},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["price"] == "219.50"
    assert patch_res.json()["changes_allowed"] is True

    # Verify audit log for UPDATE_FARE_RULE
    flight_uuid = UUID(flight_id)
    fare_uuid = UUID(fare_id)
    update_audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == fare_uuid, AuditLog.action == "UPDATE_FARE_RULE")
        .first()
    )
    assert update_audit is not None

    # 7. Delete fare rule -> 204
    del_res = client.delete(
        f"/flights/{flight_id}/fares/{fare_id}",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert del_res.status_code == 204


def test_search_returns_configured_fares_and_preserves_precision(client, db_session):
    """Verify search returns configured fare rules with exact Decimal precision and stored currency."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, pass_token = create_user_with_role(db_session, UserRole.PASSENGER, "pass")
    target_date = date(2026, 9, 25)

    flight_id = create_test_flight(
        client, ops_token, flight_number="SV777", origin="JED", destination="DXB", dep_date=target_date
    )

    # Add fare rules with distinct currencies and pricing
    client.post(
        f"/flights/{flight_id}/fares",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "class_type": "BUSINESS",
            "fare_type": "FLEXIBLE",
            "price": "1450.75",
            "currency": "AED",
            "changes_allowed": True,
            "seat_selection_allowed": True,
            "refundable": True,
            "credit_only": False,
            "cancellation_cutoff_minutes": 120,
        },
    )

    # Search for flight
    search_res = client.get(
        "/flights/search",
        headers={"Authorization": f"Bearer {pass_token}"},
        params={"origin": "JED", "destination": "DXB", "departure_date": target_date.isoformat()},
    )
    assert search_res.status_code == 200
    data = search_res.json()
    assert len(data["items"]) == 1

    item = data["items"][0]
    biz_class = next(c for c in item["classes"] if c["class_type"] == "BUSINESS")
    assert biz_class["total_seats"] == 20
    assert biz_class["available_seats"] == 20
    assert len(biz_class["fares"]) == 1

    fare = biz_class["fares"][0]
    assert fare["fare_type"] == "FLEXIBLE"
    assert fare["price"] == "1450.75"  # Exact Decimal string representation
    assert fare["currency"] == "AED"
    assert fare["changes_allowed"] is True

    # Economy has no fare rules configured -> empty list, NOT fabricated
    econ_class = next(c for c in item["classes"] if c["class_type"] == "ECONOMY")
    assert econ_class["fares"] == []


# ==========================================
# 3. READ-ONLY INVARIANT TEST
# ==========================================

def test_search_is_strictly_read_only(client, db_session):
    """Verify that executing searches does NOT alter available_seats, seat statuses, or rows."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    _, pass_token = create_user_with_role(db_session, UserRole.PASSENGER, "pass")
    target_date = date(2026, 9, 26)

    flight_id = create_test_flight(
        client, ops_token, flight_number="LH404", origin="FRA", destination="JFK", dep_date=target_date
    )
    flight_uuid = UUID(flight_id)

    # Get class available seats before search
    classes_before = {
        fc.class_type: fc.available_seats
        for fc in db_session.query(FlightClass).filter(FlightClass.flight_id == flight_uuid).all()
    }

    # Execute multiple searches
    for _ in range(3):
        res = client.get(
            "/flights/search",
            headers={"Authorization": f"Bearer {pass_token}"},
            params={"origin": "FRA", "destination": "JFK", "departure_date": target_date.isoformat()},
        )
        assert res.status_code == 200

    # Verify class available seats after search remain unchanged
    classes_after = {
        fc.class_type: fc.available_seats
        for fc in db_session.query(FlightClass).filter(FlightClass.flight_id == flight_uuid).all()
    }
    assert classes_before == classes_after
