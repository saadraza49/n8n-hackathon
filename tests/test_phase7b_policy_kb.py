from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from typing import List, Tuple
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password
from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingItem
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FareType,
    FlightClassType,
    FlightStatus,
    PolicyDocumentStatus,
    PolicyIngestionStatus,
    PolicyType,
    SeatStatus,
    UserRole,
)
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.policy import PolicyDocument
from app.models.user import User


# ============================================================================
# TEST HELPERS
# ============================================================================

def create_user_with_role(db_session: Session, role: UserRole, email_prefix: str = "user") -> Tuple[User, str]:
    now_utc = datetime.now(timezone.utc)
    user = User(
        name=f"Test {role.value}",
        email=f"{email_prefix}_{uuid4().hex[:6]}@example.com",
        password_hash=hash_password("test_password123"),
        role=role,
        created_at=now_utc - timedelta(hours=48),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(data={"sub": str(user.id), "email": user.email})
    return user, token


def create_test_flight_and_fares(
    db_session: Session,
    hours_until_dep: int = 72,
    refundable: bool = True,
    cutoff_minutes: int = 120,
    price: Decimal = Decimal("250.00"),
) -> Tuple[Flight, FareRule]:
    now_utc = datetime.now(timezone.utc)
    dep_at = now_utc + timedelta(hours=hours_until_dep)
    arr_at = dep_at + timedelta(hours=3)

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

    fare_rule = FareRule(
        flight_id=flight.id,
        class_type=FlightClassType.ECONOMY,
        fare_type=FareType.FLEXIBLE,
        price=price,
        currency="USD",
        changes_allowed=True,
        seat_selection_allowed=True,
        refundable=refundable,
        credit_only=False,
        cancellation_cutoff_minutes=cutoff_minutes,
    )
    db_session.add(fare_rule)

    db_session.commit()
    db_session.refresh(flight)
    db_session.refresh(fare_rule)
    return flight, fare_rule


def create_test_booking_with_snapshot(
    db_session: Session,
    user: User,
    flight: Flight,
    fare_rule: FareRule,
    passenger_names: List[str] = None,
    booking_status: BookingStatus = BookingStatus.CONFIRMED,
    omit_snapshot: bool = False,
) -> Booking:
    if passenger_names is None:
        passenger_names = ["Test Passenger"]

    now_utc = datetime.now(timezone.utc)
    total_amount = fare_rule.price * Decimal(len(passenger_names))

    booking = Booking(
        booking_reference=f"BK{uuid4().hex[:6].upper()}",
        user_id=user.id,
        flight_id=flight.id,
        status=booking_status,
        total_amount=total_amount,
        currency="USD",
        created_at=now_utc,
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
        seat = seats[i]
        seat.status = SeatStatus.BOOKED

        snapshot_dict = None
        if not omit_snapshot:
            snapshot_dict = {
                "fare_rule_id": str(fare_rule.id),
                "flight_id": str(fare_rule.flight_id),
                "class_type": fare_rule.class_type.value,
                "fare_type": fare_rule.fare_type.value,
                "price": str(fare_rule.price),
                "currency": fare_rule.currency,
                "refundable": fare_rule.refundable,
                "credit_only": fare_rule.credit_only,
                "changes_allowed": fare_rule.changes_allowed,
                "seat_selection_allowed": fare_rule.seat_selection_allowed,
                "cancellation_cutoff_minutes": fare_rule.cancellation_cutoff_minutes,
                "snapshotted_at": now_utc.isoformat(),
            }

        item = BookingItem(
            booking_id=booking.id,
            flight_id=flight.id,
            seat_id=seat.id,
            class_type=fare_rule.class_type,
            fare_type=fare_rule.fare_type,
            passenger_name=name,
            price=fare_rule.price,
            currency="USD",
            status=BookingItemStatus.CONFIRMED,
            fare_rule_snapshot=snapshot_dict,
        )
        db_session.add(item)

    db_session.commit()
    db_session.refresh(booking)
    return booking


# ============================================================================
# 1. POLICY DOCUMENT REGISTRY & VERSIONING TESTS
# ============================================================================

def test_policy_document_registration(client, db_session):
    """Verify registering a policy document with raw text content calculates SHA-256 and writes audit log."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_reg")
    doc_text = "FMS General Cancellation Policy: Passengers may cancel up to 2 hours before flight."
    expected_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()

    res = client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": "fms_cancellation_policy.md",
            "policy_type": "CANCELLATION",
            "version": "1.0",
            "source": "Operations Manual 2026",
            "source_url": "https://fms.internal/policies/cancellation_v1.md",
            "content": doc_text,
            "chunk_count": 4,
            "metadata_info": {"author": "Compliance Officer", "category": "Core Policy"},
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["document_name"] == "fms_cancellation_policy.md"
    assert data["policy_type"] == "CANCELLATION"
    assert data["version"] == "1.0"
    assert data["document_hash"] == expected_hash
    assert data["status"] == "ACTIVE"
    assert data["ingestion_status"] == "PENDING"
    assert data["content_length"] == len(doc_text.encode("utf-8"))
    assert data["chunk_count"] == 4

    # Verify audit log entry
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == UUID(data["id"]), AuditLog.action == "POLICY_DOCUMENT_REGISTERED")
        .first()
    )
    assert audit is not None
    assert audit.new_values["version"] == "1.0"


def test_policy_document_duplicate_version_rejected_409(client, db_session):
    """Verify attempting to register identical (document_name, version) returns 409 Conflict."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_dup")
    doc_name = f"baggage_rules_{uuid4().hex[:6]}.md"

    # Register initial
    client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": doc_name,
            "policy_type": "GENERAL",
            "version": "1.0",
            "source": "Carrier Manual",
            "content": "Baggage allowance is 20kg.",
        },
    )

    # Register duplicate
    res_dup = client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": doc_name,
            "policy_type": "GENERAL",
            "version": "1.0",
            "source": "Carrier Manual",
            "content": "Different baggage content.",
        },
    )
    assert res_dup.status_code == 409
    assert "already registered" in res_dup.json()["detail"]


def test_policy_document_versioning_and_auto_superseding(client, db_session):
    """Verify registering version 2.0 with supersede_previous_active=True marks version 1.0 as SUPERSEDED."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_sup")
    doc_name = f"rebooking_policy_{uuid4().hex[:6]}.md"

    # Register version 1.0
    res1 = client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": doc_name,
            "policy_type": "REBOOKING",
            "version": "1.0",
            "source": "Service Terms",
            "content": "Version 1 rebooking terms.",
        },
    )
    assert res1.status_code == 201
    doc1_id = res1.json()["id"]

    # Register version 2.0 (supersede_previous_active=True by default)
    res2 = client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": doc_name,
            "policy_type": "REBOOKING",
            "version": "2.0",
            "source": "Service Terms 2026 Rev 2",
            "content": "Version 2 updated rebooking terms.",
        },
    )
    assert res2.status_code == 201
    assert res2.json()["status"] == "ACTIVE"

    # Verify version 1.0 is now SUPERSEDED
    res_get1 = client.get(f"/policy/documents/{doc1_id}", headers={"Authorization": f"Bearer {ops_token}"})
    assert res_get1.status_code == 200
    assert res_get1.json()["status"] == "SUPERSEDED"


def test_policy_document_update_and_ingestion_state_change(client, db_session):
    """Verify updating ingestion_status (e.g. n8n setting COMPLETED) updates timestamps and audit log."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_ingest")
    res = client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": f"waitlist_terms_{uuid4().hex[:6]}.md",
            "policy_type": "WAITLIST",
            "version": "1.0",
            "source": "Waitlist Spec",
            "content": "Waitlist priority is determined by tier and claim window.",
        },
    )
    doc_id = res.json()["id"]

    # n8n marks processing
    client.patch(
        f"/policy/documents/{doc_id}",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={"ingestion_status": "PROCESSING"},
    )

    # n8n marks completed with chunk count
    res_done = client.patch(
        f"/policy/documents/{doc_id}",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "ingestion_status": "COMPLETED",
            "chunk_count": 8,
            "metadata_info": {"pinecone_namespace": "fms-policies-v1"},
        },
    )
    assert res_done.status_code == 200
    data = res_done.json()
    assert data["ingestion_status"] == "COMPLETED"
    assert data["chunk_count"] == 8
    assert data["retrieved_at"] is not None
    assert data["metadata_info"]["pinecone_namespace"] == "fms-policies-v1"


def test_policy_document_filtering_and_pagination(client, db_session):
    """Verify listing documents with policy_type, status, and pagination filters."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_filter")
    prefix = uuid4().hex[:6]

    client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": f"filter_{prefix}_cancels.md",
            "policy_type": "CANCELLATION",
            "version": "1.0",
            "source": "Filter Test",
            "content": "Cancellation rules.",
        },
    )
    client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": f"filter_{prefix}_refunds.md",
            "policy_type": "REFUND",
            "version": "1.0",
            "source": "Filter Test",
            "content": "Refund monetary rules.",
        },
    )

    # Filter by CANCELLATION
    res_cancels = client.get(
        f"/policy/documents?policy_type=CANCELLATION&search={prefix}",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    assert res_cancels.status_code == 200
    data = res_cancels.json()
    assert data["total"] == 1
    assert data["items"][0]["policy_type"] == "CANCELLATION"


# ============================================================================
# 2. CHANGE DETECTION SERVICE TESTS (N8N INTEGRATION)
# ============================================================================

def test_detect_policy_document_changes_flow(client, db_session):
    """
    Verify change detection matrix:
    - NEW_DOCUMENT: document does not exist in registry -> requires_embedding=True
    - UNCHANGED: document content hash matches active version -> requires_embedding=False
    - CONTENT_CHANGED: document hash differs from active version -> requires_embedding=True
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_change")
    doc_name = f"change_test_{uuid4().hex[:6]}.md"
    original_text = "Standard check-in opens 24h prior to flight."

    # Register initial active document
    client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {ops_token}"},
        json={
            "document_name": doc_name,
            "policy_type": "CHECK_IN",
            "version": "1.0",
            "source": "Ground Operations",
            "content": original_text,
        },
    )

    # Detect batch of 3 documents:
    # 1. Unknown new doc
    # 2. Unchanged existing doc
    # 3. Modified existing doc
    batch_payload = {
        "documents": [
            {
                "document_name": "completely_new_policy.md",
                "content": "Some new policy text.",
            },
            {
                "document_name": doc_name,
                "content": original_text,
            },
            {
                "document_name": doc_name,
                "content": "Modified check-in opens 48h prior to flight.",
            },
        ]
    }

    res = client.post(
        "/policy/documents/detect-changes",
        headers={"Authorization": f"Bearer {ops_token}"},
        json=batch_payload,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_evaluated"] == 3
    assert data["requires_embedding_count"] == 2

    res_map = {r["document_name"] + str(r["requires_embedding"]): r for r in data["results"]}

    # New doc
    new_res = [r for r in data["results"] if r["document_name"] == "completely_new_policy.md"][0]
    assert new_res["change_status"] == "NEW_DOCUMENT"
    assert new_res["requires_embedding"] is True

    # Unchanged doc
    unchanged_res = [r for r in data["results"] if r["document_name"] == doc_name and not r["requires_embedding"]][0]
    assert unchanged_res["change_status"] == "UNCHANGED"
    assert unchanged_res["requires_embedding"] is False

    # Changed doc
    changed_res = [r for r in data["results"] if r["document_name"] == doc_name and r["requires_embedding"]][0]
    assert changed_res["change_status"] == "CONTENT_CHANGED"
    assert changed_res["requires_embedding"] is True


# ============================================================================
# 3. AUTHORITATIVE BOOKING RAG CONTEXT & FARE-RULE AUTHORITY TESTS
# ============================================================================

def test_booking_rag_context_authoritative_retrieval(client, db_session):
    """
    Verify /rag/bookings/{id}/context returns:
    1. Authoritative booking and passenger details.
    2. Flight origin, destination, and departure.
    3. Item-level fare rule snapshots.
    4. Derived policy context: refundable, cutoff deadline, hours to departure, cancellation allowed.
    """
    passenger, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_rag")
    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=72, refundable=True, cutoff_minutes=120)
    booking = create_test_booking_with_snapshot(db_session, passenger, flight, fare_rule)

    res = client.get(
        f"/rag/bookings/{booking.id}/context",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert res.status_code == 200
    data = res.json()

    # Verify facts
    assert data["booking"]["booking_reference"] == booking.booking_reference
    assert data["passenger"]["email"] == passenger.email
    assert data["flight"]["flight_number"] == flight.flight_number
    assert len(data["items"]) == 1
    assert data["items"][0]["fare_rule_snapshot"]["refundable"] is True
    assert data["items"][0]["fare_rule_snapshot"]["cancellation_cutoff_minutes"] == 120

    # Verify policy context calculation
    ctx = data["policy_context"]
    assert ctx["refundable"] is True
    assert ctx["is_before_cancellation_cutoff"] is True
    assert ctx["is_cancellation_allowed"] is True
    assert ctx["hours_until_departure"] > 70.0
    assert "Eligible for monetary refund" in ctx["policy_summary"]


def test_booking_rag_context_alias_endpoint(client, db_session):
    """Verify /policy/context/booking/{booking_id} functions as an identical alias to /rag/bookings/{booking_id}/context."""
    passenger, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_alias")
    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=48)
    booking = create_test_booking_with_snapshot(db_session, passenger, flight, fare_rule)

    res_rag = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {p_token}"})
    res_alias = client.get(f"/policy/context/booking/{booking.id}", headers={"Authorization": f"Bearer {p_token}"})

    assert res_rag.status_code == 200
    assert res_alias.status_code == 200
    assert res_rag.json()["booking"]["id"] == res_alias.json()["booking"]["id"]


def test_fare_rule_snapshot_authority_against_live_rule_tampering(client, db_session):
    """
    CRITICAL INVARIANT TEST:
    1. Booking is created with FLEXIBLE fare ($250, refundable=True, cutoff=120m).
    2. Admin/operator updates live FareRule to price=$600, refundable=False, cutoff=0m.
    3. Customer's RAG context MUST STILL reflect the purchased terms ($250, refundable=True, cutoff=120m).
    Live fare rule edits must NEVER retroactively strip customer's purchased rights!
    """
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_rule_change")
    passenger, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_tamper")
    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=72, refundable=True, cutoff_minutes=120, price=Decimal("250.00"))
    booking = create_test_booking_with_snapshot(db_session, passenger, flight, fare_rule)

    # Verify initial snapshot
    res_initial = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {p_token}"})
    assert res_initial.json()["items"][0]["fare_rule_snapshot"]["refundable"] is True
    assert Decimal(str(res_initial.json()["items"][0]["fare_rule_snapshot"]["price"])) == Decimal("250.00")

    # Operator updates live FareRule in database
    fare_rule.price = Decimal("600.00")
    fare_rule.refundable = False
    fare_rule.credit_only = False
    fare_rule.cancellation_cutoff_minutes = 0
    db_session.commit()

    # Re-fetch RAG context: MUST STILL BE 250.00 and REFUNDABLE!
    res_after = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {p_token}"})
    assert res_after.status_code == 200
    after_data = res_after.json()
    snapshot = after_data["items"][0]["fare_rule_snapshot"]
    assert Decimal(str(snapshot["price"])) == Decimal("250.00")
    assert snapshot["refundable"] is True
    assert snapshot["cancellation_cutoff_minutes"] == 120
    assert after_data["policy_context"]["refundable"] is True
    assert after_data["policy_context"]["is_cancellation_allowed"] is True


def test_cancellation_honors_fare_rule_snapshot_over_live_rule(client, db_session):
    """Verify that cancellation service respects fare_rule_snapshot even when live fare rule was made non-refundable."""
    passenger, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_cancel_snap")
    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=72, refundable=True, cutoff_minutes=120)
    booking = create_test_booking_with_snapshot(db_session, passenger, flight, fare_rule)

    # Mutate live rule to non-refundable
    fare_rule.refundable = False
    fare_rule.credit_only = False
    db_session.commit()

    # Cancel booking
    res_cancel = client.post(
        f"/bookings/{booking.id}/cancel",
        headers={"Authorization": f"Bearer {p_token}"},
        json={},
    )
    assert res_cancel.status_code == 200
    cancel_data = res_cancel.json()
    assert Decimal(str(cancel_data["total_refund_amount"])) == Decimal("250.00")
    assert cancel_data["refunds"][0]["refund_type"] == "MONETARY"


def test_booking_rag_context_ownership_and_rbac(client, db_session):
    """Verify Passenger 2 cannot access Passenger 1's RAG context (403 Forbidden). Ops and Admin can access."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops_acc")
    _, admin_token = create_user_with_role(db_session, UserRole.SUPER_ADMIN, "admin_acc")
    p1, _ = create_user_with_role(db_session, UserRole.PASSENGER, "p_owner")
    _, p2_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_intruder")

    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=48)
    booking = create_test_booking_with_snapshot(db_session, p1, flight, fare_rule)

    # Passenger 2 forbidden (IDOR protection)
    res_intruder = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {p2_token}"})
    assert res_intruder.status_code == 403
    assert "permission" in res_intruder.json()["detail"]

    # Ops Agent allowed
    res_ops = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {ops_token}"})
    assert res_ops.status_code == 200

    # Super Admin allowed
    res_admin = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {admin_token}"})
    assert res_admin.status_code == 200


def test_booking_rag_context_nonexistent_booking_404(client, db_session):
    """Verify querying nonexistent booking returns 404 Not Found."""
    _, ops_token = create_user_with_role(db_session, UserRole.OPS_AGENT, "ops")
    res = client.get(f"/rag/bookings/{uuid4()}/context", headers={"Authorization": f"Bearer {ops_token}"})
    assert res.status_code == 404


def test_booking_rag_context_cancelled_booking(client, db_session):
    """Verify querying RAG context for a cancelled booking correctly reports is_cancellation_allowed=False."""
    passenger, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_canc")
    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=48)
    booking = create_test_booking_with_snapshot(db_session, passenger, flight, fare_rule, booking_status=BookingStatus.CANCELLED)

    res = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {p_token}"})
    assert res.status_code == 200
    data = res.json()
    assert data["booking"]["status"] == "CANCELLED"
    assert data["policy_context"]["is_cancellation_allowed"] is False
    assert "already CANCELLED" in data["policy_context"]["policy_summary"]


def test_booking_rag_context_multi_item_group_booking(client, db_session):
    """Verify RAG context accurately captures multi-item group booking with multiple seats."""
    passenger, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_group")
    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=96)
    booking = create_test_booking_with_snapshot(
        db_session,
        passenger,
        flight,
        fare_rule,
        passenger_names=["Alice Smith", "Bob Smith", "Charlie Smith"],
    )

    res = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {p_token}"})
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 3
    names = [it["passenger_name"] for it in data["items"]]
    assert "Alice Smith" in names
    assert "Bob Smith" in names
    assert "Charlie Smith" in names


def test_policy_document_rbac_restrictions(client, db_session):
    """Verify regular passengers are forbidden from modifying or querying internal policy registry."""
    _, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_unauth")

    # Attempt registration
    res1 = client.post(
        "/policy/documents",
        headers={"Authorization": f"Bearer {p_token}"},
        json={
            "document_name": "test.md",
            "policy_type": "GENERAL",
            "version": "1.0",
            "source": "Test",
            "content": "Content",
        },
    )
    assert res1.status_code == 403

    # Attempt list
    res2 = client.get("/policy/documents", headers={"Authorization": f"Bearer {p_token}"})
    assert res2.status_code == 403

    # Attempt change detection
    res3 = client.post(
        "/policy/documents/detect-changes",
        headers={"Authorization": f"Bearer {p_token}"},
        json={"documents": [{"document_name": "test.md", "content": "text"}]},
    )
    assert res3.status_code == 403


def test_legacy_booking_fallback_without_snapshot(client, db_session):
    """Verify older legacy bookings where fare_rule_snapshot is NULL gracefully synthesize snapshot from FareRule."""
    passenger, p_token = create_user_with_role(db_session, UserRole.PASSENGER, "p_legacy")
    flight, fare_rule = create_test_flight_and_fares(db_session, hours_until_dep=48, refundable=True, cutoff_minutes=60)
    booking = create_test_booking_with_snapshot(db_session, passenger, flight, fare_rule, omit_snapshot=True)

    res = client.get(f"/rag/bookings/{booking.id}/context", headers={"Authorization": f"Bearer {p_token}"})
    assert res.status_code == 200
    data = res.json()
    snapshot = data["items"][0]["fare_rule_snapshot"]
    assert snapshot["refundable"] is True
    assert snapshot["cancellation_cutoff_minutes"] == 60
    assert data["policy_context"]["refundable"] is True
