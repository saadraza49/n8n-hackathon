from datetime import datetime, timedelta, timezone
from decimal import Decimal
import random
import string
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingItem
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FareType,
    FlightClassType,
    FlightStatus,
    SeatStatus,
    UserRole,
    WaitlistStatus,
)
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User
from app.models.waitlist import WaitlistEntry
from app.schemas.waitlist import (
    WaitlistClaimRequest,
    WaitlistClaimResponse,
    WaitlistEntryResponse,
    WaitlistFlightQueueResponse,
    WaitlistJoinRequest,
)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _generate_booking_reference(db: Session) -> str:
    """Generate a collision-resistant unique 6-character alphanumeric reference."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(100):
        ref = "".join(random.choices(chars, k=6))
        exists = db.query(Booking).filter(Booking.booking_reference == ref).first()
        if not exists:
            return ref
    raise RuntimeError("Failed to generate unique booking reference")


def _compute_queue_position(db: Session, entry: WaitlistEntry) -> Optional[int]:
    """
    Dynamically compute deterministic 1-indexed queue position for a WAITING entry.
    Priority rule: (priority_score DESC, joined_at ASC, id ASC).
    Returns None if the entry is no longer WAITING.
    """
    if entry.status != WaitlistStatus.WAITING:
        return None

    ahead_count = (
        db.query(func.count(WaitlistEntry.id))
        .filter(
            WaitlistEntry.flight_id == entry.flight_id,
            WaitlistEntry.class_type == entry.class_type,
            WaitlistEntry.status == WaitlistStatus.WAITING,
            or_(
                WaitlistEntry.priority_score > entry.priority_score,
                and_(
                    WaitlistEntry.priority_score == entry.priority_score,
                    WaitlistEntry.joined_at < entry.joined_at,
                ),
                and_(
                    WaitlistEntry.priority_score == entry.priority_score,
                    WaitlistEntry.joined_at == entry.joined_at,
                    WaitlistEntry.id < entry.id,
                ),
            ),
        )
        .scalar()
    )
    return (ahead_count or 0) + 1


def _serialize_waitlist_entry(db: Session, entry: WaitlistEntry) -> WaitlistEntryResponse:
    """Serialize a WaitlistEntry with its derived queue position."""
    pos = _compute_queue_position(db, entry)
    return WaitlistEntryResponse(
        id=entry.id,
        flight_id=entry.flight_id,
        passenger_id=entry.passenger_id,
        class_type=entry.class_type,
        entry_type=entry.entry_type,
        status=entry.status,
        priority_score=entry.priority_score,
        queue_position=pos,
        joined_at=entry.joined_at,
        promoted_at=entry.promoted_at,
        claim_deadline=entry.claim_deadline,
        claimed_at=entry.claimed_at,
        cancelled_at=entry.cancelled_at,
        promoted_seat_id=entry.promoted_seat_id,
        booking_id=entry.booking_id,
        notes=entry.notes,
    )


def join_waitlist(
    db: Session,
    flight_id: UUID,
    payload: WaitlistJoinRequest,
    current_user: User,
) -> WaitlistEntryResponse:
    """
    Add authenticated passenger to a class-specific waitlist when the cabin class is full.
    Enforces authoritative inventory validation, duplicate active entry prevention,
    idempotency replay, and audit logging.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Idempotency replay check
    if payload.idempotency_key:
        existing_idempotent = (
            db.query(WaitlistEntry)
            .filter(
                WaitlistEntry.passenger_id == current_user.id,
                WaitlistEntry.idempotency_key == payload.idempotency_key,
            )
            .first()
        )
        if existing_idempotent:
            if existing_idempotent.flight_id == flight_id and existing_idempotent.class_type == payload.class_type:
                return _serialize_waitlist_entry(db, existing_idempotent)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key reused with different waitlist parameters",
            )

    # 2. Flight validation
    flight = db.query(Flight).filter(Flight.id == flight_id).first()
    if not flight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Flight '{flight_id}' not found",
        )

    if flight.status in (FlightStatus.CANCELLED, FlightStatus.COMPLETED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Flight status is '{flight.status.value}'; cannot join waitlist",
        )

    if _ensure_utc(flight.departure_at) <= now_utc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Flight has already departed",
        )

    # 3. Class validation & Authoritative Inventory Check
    flight_class = (
        db.query(FlightClass)
        .filter(
            FlightClass.flight_id == flight_id,
            FlightClass.class_type == payload.class_type,
        )
        .first()
    )
    if not flight_class:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Class '{payload.class_type.value}' is not configured for flight '{flight_id}'",
        )

    if flight_class.available_seats > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Seats are currently available in class '{payload.class_type.value}' ({flight_class.available_seats} remaining). Direct booking is required.",
        )

    # 4. Duplicate Active Entry Protection (Application + DB partial index)
    existing_active = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.passenger_id == current_user.id,
            WaitlistEntry.flight_id == flight_id,
            WaitlistEntry.class_type == payload.class_type,
            WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.PROMOTED, WaitlistStatus.CLAIMED]),
        )
        .first()
    )
    if existing_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have an active waitlist entry ({existing_active.status.value}) for class '{payload.class_type.value}' on this flight.",
        )

    # 5. Persist Waitlist Entry
    entry = WaitlistEntry(
        id=uuid4(),
        flight_id=flight_id,
        passenger_id=current_user.id,
        class_type=payload.class_type,
        entry_type=payload.entry_type,
        status=WaitlistStatus.WAITING,
        priority_score=0,
        joined_at=now_utc,
        idempotency_key=payload.idempotency_key,
        notes=payload.notes,
    )
    db.add(entry)

    # 6. Audit Logging
    audit = AuditLog(
        id=uuid4(),
        user_id=current_user.id,
        action="WAITLIST_JOINED",
        entity_type="WAITLIST_ENTRY",
        entity_id=entry.id,
        new_values={
            "flight_id": str(flight_id),
            "passenger_id": str(current_user.id),
            "class_type": payload.class_type.value,
            "entry_type": payload.entry_type.value,
            "status": WaitlistStatus.WAITING.value,
        },
    )
    db.add(audit)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have an active waitlist entry for class '{payload.class_type.value}' on this flight.",
        )

    db.refresh(entry)

    return _serialize_waitlist_entry(db, entry)


def promote_next_passenger(
    db: Session,
    flight_id: UUID,
    class_type: FlightClassType,
    claim_window_minutes: int,
    current_user: User,
) -> Optional[WaitlistEntryResponse]:
    """
    Promote the highest priority eligible WAITING passenger when inventory is available.
    Uses PostgreSQL row-level locking (FOR UPDATE SKIP LOCKED) to prevent worker race conditions.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Lock Next Eligible WAITING Passenger
    candidate_entry = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.flight_id == flight_id,
            WaitlistEntry.class_type == class_type,
            WaitlistEntry.status == WaitlistStatus.WAITING,
        )
        .order_by(
            WaitlistEntry.priority_score.desc(),
            WaitlistEntry.joined_at.asc(),
            WaitlistEntry.id.asc(),
        )
        .with_for_update(skip_locked=True)
        .first()
    )
    if not candidate_entry:
        return None

    # 2. Lock Physical Available Seat
    available_seat = (
        db.query(FlightSeat)
        .filter(
            FlightSeat.flight_id == flight_id,
            FlightSeat.class_type == class_type,
            FlightSeat.status == SeatStatus.AVAILABLE,
        )
        .order_by(FlightSeat.seat_number.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if not available_seat:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No available physical seats in class '{class_type.value}' to promote waitlisted passenger",
        )

    # 3. Lock Flight Class and decrement available seats
    flight_class = (
        db.query(FlightClass)
        .filter(
            FlightClass.flight_id == flight_id,
            FlightClass.class_type == class_type,
        )
        .with_for_update()
        .first()
    )
    if not flight_class or flight_class.available_seats <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No available class inventory to promote passenger",
        )

    claim_deadline = now_utc + timedelta(minutes=claim_window_minutes)

    # 4. Atomic Transitions: Seat -> HELD, Class -> decremented, Waitlist -> PROMOTED
    seat_updated = (
        db.query(FlightSeat)
        .filter(
            FlightSeat.id == available_seat.id,
            FlightSeat.status == SeatStatus.AVAILABLE,
        )
        .update(
            {
                FlightSeat.status: SeatStatus.HELD,
                FlightSeat.hold_expires_at: claim_deadline,
            },
            synchronize_session=False,
        )
    )
    if seat_updated == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Seat '{available_seat.seat_number}' was claimed by another worker",
        )

    entry_updated = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.id == candidate_entry.id,
            WaitlistEntry.status == WaitlistStatus.WAITING,
        )
        .update(
            {
                WaitlistEntry.status: WaitlistStatus.PROMOTED,
                WaitlistEntry.promoted_at: now_utc,
                WaitlistEntry.claim_deadline: claim_deadline,
                WaitlistEntry.promoted_seat_id: available_seat.id,
            },
            synchronize_session=False,
        )
    )
    if entry_updated == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Waitlist entry was already promoted or cancelled by another worker",
        )

    flight_class.available_seats = max(0, flight_class.available_seats - 1)
    candidate_entry.status = WaitlistStatus.PROMOTED
    candidate_entry.promoted_at = now_utc
    candidate_entry.claim_deadline = claim_deadline
    candidate_entry.promoted_seat_id = available_seat.id

    # 5. Audit Logging
    audit = AuditLog(
        id=uuid4(),
        user_id=current_user.id,
        action="WAITLIST_PROMOTED",
        entity_type="WAITLIST_ENTRY",
        entity_id=candidate_entry.id,
        old_values={"status": WaitlistStatus.WAITING.value},
        new_values={
            "status": WaitlistStatus.PROMOTED.value,
            "promoted_seat_id": str(available_seat.id),
            "seat_number": available_seat.seat_number,
            "claim_deadline": claim_deadline.isoformat(),
        },
    )
    db.add(audit)

    db.commit()
    db.refresh(candidate_entry)

    return _serialize_waitlist_entry(db, candidate_entry)


def claim_promoted_seat(
    db: Session,
    waitlist_id: UUID,
    payload: WaitlistClaimRequest,
    current_user: User,
) -> WaitlistClaimResponse:
    """
    Passenger claims a promoted seat opportunity within the claim window.
    Converts promotion into a confirmed Booking & BookingItem using Phase 3A architecture.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Lock Waitlist Entry Row
    entry = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.id == waitlist_id)
        .with_for_update()
        .first()
    )
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Waitlist entry '{waitlist_id}' not found",
        )

    # 2. Ownership verification
    if current_user.role == UserRole.PASSENGER and entry.passenger_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to claim this waitlist entry",
        )

    # 3. Status validation
    if entry.status != WaitlistStatus.PROMOTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot claim waitlist entry with status '{entry.status.value}' (must be PROMOTED)",
        )

    # 4. Claim deadline evaluation
    if entry.claim_deadline and _ensure_utc(entry.claim_deadline) < now_utc:
        # Expire entry and release held seat back to AVAILABLE
        entry.status = WaitlistStatus.EXPIRED
        if entry.promoted_seat_id:
            seat = (
                db.query(FlightSeat)
                .filter(FlightSeat.id == entry.promoted_seat_id)
                .with_for_update()
                .first()
            )
            if seat and seat.status == SeatStatus.HELD:
                seat.status = SeatStatus.AVAILABLE
                seat.hold_expires_at = None
                flight_class = (
                    db.query(FlightClass)
                    .filter(
                        FlightClass.flight_id == entry.flight_id,
                        FlightClass.class_type == entry.class_type,
                    )
                    .with_for_update()
                    .first()
                )
                if flight_class:
                    flight_class.available_seats = min(
                        flight_class.total_seats, flight_class.available_seats + 1
                    )

        db.add(AuditLog(
            id=uuid4(),
            user_id=current_user.id,
            action="WAITLIST_EXPIRED",
            entity_type="WAITLIST_ENTRY",
            entity_id=entry.id,
            old_values={"status": WaitlistStatus.PROMOTED.value},
            new_values={"status": WaitlistStatus.EXPIRED.value},
        ))
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Claim window has expired. The promotion is no longer valid.",
        )

    # 5. Authoritative Fare Rule Lookup
    fare_rule = (
        db.query(FareRule)
        .filter(
            FareRule.flight_id == entry.flight_id,
            FareRule.class_type == entry.class_type,
            FareRule.fare_type == payload.fare_type,
        )
        .first()
    )
    if not fare_rule:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No fare rule configured for class '{entry.class_type.value}' and fare type '{payload.fare_type.value}'",
        )

    # 6. Lock Promoted Seat and Transition to BOOKED
    seat = (
        db.query(FlightSeat)
        .filter(FlightSeat.id == entry.promoted_seat_id)
        .with_for_update()
        .first()
    )
    if not seat or seat.status != SeatStatus.HELD:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Promoted seat is no longer held for this claim",
        )

    seat.status = SeatStatus.BOOKED
    seat.hold_expires_at = None

    # 7. Create Confirmed Booking (Phase 3A Model)
    booking_ref = _generate_booking_reference(db)
    passenger_name = payload.passenger_name or current_user.name

    booking = Booking(
        id=uuid4(),
        booking_reference=booking_ref,
        user_id=entry.passenger_id,
        flight_id=entry.flight_id,
        status=BookingStatus.CONFIRMED,
        total_amount=fare_rule.price,
        currency=fare_rule.currency,
        idempotency_key=payload.idempotency_key,
        hold_expires_at=None,
    )
    db.add(booking)
    db.flush()

    # 8. Create Confirmed Booking Item
    item = BookingItem(
        id=uuid4(),
        booking_id=booking.id,
        flight_id=entry.flight_id,
        seat_id=seat.id,
        class_type=entry.class_type,
        fare_type=payload.fare_type,
        status=BookingItemStatus.CONFIRMED,
        passenger_name=passenger_name,
        price=fare_rule.price,
        currency=fare_rule.currency,
    )
    db.add(item)

    # 9. Transition Waitlist Entry to CONVERTED
    entry.status = WaitlistStatus.CONVERTED
    entry.claimed_at = now_utc
    entry.booking_id = booking.id

    # 10. Audit Logging
    audit = AuditLog(
        id=uuid4(),
        user_id=current_user.id,
        action="WAITLIST_CONVERTED",
        entity_type="WAITLIST_ENTRY",
        entity_id=entry.id,
        old_values={"status": WaitlistStatus.PROMOTED.value},
        new_values={
            "status": WaitlistStatus.CONVERTED.value,
            "booking_id": str(booking.id),
            "booking_reference": booking.booking_reference,
            "seat_number": seat.seat_number,
            "total_amount": str(booking.total_amount),
        },
    )
    db.add(audit)

    db.commit()

    return WaitlistClaimResponse(
        waitlist_id=entry.id,
        booking_id=booking.id,
        booking_reference=booking.booking_reference,
        seat_id=seat.id,
        seat_number=seat.seat_number,
        class_type=entry.class_type,
        fare_type=payload.fare_type,
        total_amount=booking.total_amount,
        currency=booking.currency,
        message="Waitlist promotion successfully claimed and converted into confirmed booking.",
    )


def cancel_waitlist_entry(
    db: Session,
    waitlist_id: UUID,
    current_user: User,
) -> WaitlistEntryResponse:
    """
    Cancel an active waitlist entry.
    If the entry was PROMOTED, atomically releases the held physical seat and restores class inventory.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Fetch Waitlist Entry
    entry = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.id == waitlist_id)
        .with_for_update()
        .first()
    )
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Waitlist entry '{waitlist_id}' not found",
        )

    # 2. Ownership verification
    if current_user.role == UserRole.PASSENGER and entry.passenger_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to cancel this waitlist entry",
        )

    # 3. Status validation
    if entry.status not in (WaitlistStatus.WAITING, WaitlistStatus.PROMOTED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel waitlist entry with status '{entry.status.value}'",
        )

    was_promoted = entry.status == WaitlistStatus.PROMOTED
    old_status = entry.status.value

    # 4. Atomic conditional status update on waitlist entry
    updated_count = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.id == waitlist_id,
            WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.PROMOTED]),
        )
        .update(
            {
                WaitlistEntry.status: WaitlistStatus.CANCELLED,
                WaitlistEntry.cancelled_at: now_utc,
            },
            synchronize_session=False,
        )
    )
    if updated_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Waitlist entry is already cancelled or modified",
        )

    entry.status = WaitlistStatus.CANCELLED
    entry.cancelled_at = now_utc

    # 5. If PROMOTED, release the held seat and restore inventory
    if was_promoted and entry.promoted_seat_id:
        seat_updated = (
            db.query(FlightSeat)
            .filter(
                FlightSeat.id == entry.promoted_seat_id,
                FlightSeat.status == SeatStatus.HELD,
            )
            .update(
                {
                    FlightSeat.status: SeatStatus.AVAILABLE,
                    FlightSeat.hold_expires_at: None,
                },
                synchronize_session=False,
            )
        )
        if seat_updated > 0:
            flight_class = (
                db.query(FlightClass)
                .filter(
                    FlightClass.flight_id == entry.flight_id,
                    FlightClass.class_type == entry.class_type,
                )
                .with_for_update()
                .first()
            )
            if flight_class:
                flight_class.available_seats = min(
                    flight_class.total_seats, flight_class.available_seats + 1
                )

    # 6. Audit Logging
    audit = AuditLog(
        id=uuid4(),
        user_id=current_user.id,
        action="WAITLIST_CANCELLED",
        entity_type="WAITLIST_ENTRY",
        entity_id=entry.id,
        old_values={"status": old_status},
        new_values={
            "status": WaitlistStatus.CANCELLED.value,
            "cancelled_at": now_utc.isoformat(),
        },
    )
    db.add(audit)

    db.commit()
    db.refresh(entry)

    return _serialize_waitlist_entry(db, entry)


def get_passenger_waitlists(
    db: Session,
    passenger_id: UUID,
) -> List[WaitlistEntryResponse]:
    """Retrieve all waitlist entries for a passenger."""
    entries = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.passenger_id == passenger_id)
        .order_by(WaitlistEntry.created_at.desc())
        .all()
    )
    return [_serialize_waitlist_entry(db, e) for e in entries]


def get_flight_waitlist_queue(
    db: Session,
    flight_id: UUID,
    class_type: Optional[FlightClassType] = None,
) -> WaitlistFlightQueueResponse:
    """Retrieve waitlist queue for a flight (Ops/Admin inspection)."""
    flight = db.query(Flight).filter(Flight.id == flight_id).first()
    if not flight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Flight '{flight_id}' not found",
        )

    query = db.query(WaitlistEntry).filter(WaitlistEntry.flight_id == flight_id)
    if class_type:
        query = query.filter(WaitlistEntry.class_type == class_type)

    entries = query.order_by(
        WaitlistEntry.priority_score.desc(),
        WaitlistEntry.joined_at.asc(),
        WaitlistEntry.id.asc(),
    ).all()

    total_waiting = sum(1 for e in entries if e.status == WaitlistStatus.WAITING)
    total_promoted = sum(1 for e in entries if e.status == WaitlistStatus.PROMOTED)

    return WaitlistFlightQueueResponse(
        flight_id=flight.id,
        flight_number=flight.flight_number,
        class_type=class_type,
        total_waiting=total_waiting,
        total_promoted=total_promoted,
        entries=[_serialize_waitlist_entry(db, e) for e in entries],
    )


def get_waitlist_entry_by_id(
    db: Session,
    waitlist_id: UUID,
    current_user: User,
) -> WaitlistEntryResponse:
    """Retrieve details of a specific waitlist entry."""
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.id == waitlist_id).first()
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Waitlist entry '{waitlist_id}' not found",
        )

    if current_user.role == UserRole.PASSENGER and entry.passenger_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this waitlist entry",
        )

    return _serialize_waitlist_entry(db, entry)
