from datetime import datetime, timedelta, timezone
from decimal import Decimal
import random
from typing import List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.models.booking import Booking, BookingItem
from app.models.enums import BookingStatus, FareType, FlightClassType, FlightStatus, SeatStatus, UserRole
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User
from app.schemas.booking import (
    BookingHoldCreate,
    BookingItemHoldRequest,
    BookingItemResponse,
    BookingPagination,
    BookingResponse,
)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Helper to ensure datetime is timezone-aware in UTC for safe comparisons across dialects."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _generate_booking_reference(db: Session) -> str:
    """Generate a collision-resistant 6-character uppercase alphanumeric booking reference."""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # unambiguous alphanumeric

    for _ in range(20):
        ref = "".join(random.choices(chars, k=6))
        exists = db.query(Booking).filter(Booking.booking_reference == ref).first()
        if not exists:
            return ref
    # Fallback to random hex segment if loops exhausted
    import uuid
    return uuid.uuid4().hex[:6].upper()


def _format_booking_response(booking: Booking) -> BookingResponse:
    """Helper to convert Booking model and its items into BookingResponse with seat numbers."""
    item_responses = []
    for item in booking.items:
        seat_num = item.seat.seat_number if item.seat else None
        item_responses.append(
            BookingItemResponse(
                id=item.id,
                booking_id=item.booking_id,
                flight_id=item.flight_id,
                seat_id=item.seat_id,
                seat_number=seat_num,
                class_type=item.class_type,
                fare_type=item.fare_type,
                passenger_name=item.passenger_name,
                price=item.price,
                currency=item.currency,
                created_at=item.created_at,
            )
        )

    return BookingResponse(
        id=booking.id,
        booking_reference=booking.booking_reference,
        user_id=booking.user_id,
        flight_id=booking.flight_id,
        status=booking.status,
        total_amount=booking.total_amount,
        currency=booking.currency,
        idempotency_key=booking.idempotency_key,
        hold_expires_at=booking.hold_expires_at,
        created_at=booking.created_at,
        updated_at=booking.updated_at,
        items=item_responses,
    )


def create_hold(
    db: Session,
    payload: BookingHoldCreate,
    current_user: User,
) -> BookingResponse:
    """
    Creates a temporary seat hold and a PENDING booking.
    
    Guarantees:
    - Atomicity: locks physical seats and flight class inventory via SELECT ... FOR UPDATE.
    - Idempotency: exact duplicate requests return the existing hold; conflicting payloads return 409.
    - Consistency: decrements flight_classes.available_seats for each seat transitioning from AVAILABLE.
    - Precision: snapshots authoritative price and currency from fare_rules.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Idempotency check
    if payload.idempotency_key:
        existing_booking = (
            db.query(Booking)
            .filter(
                Booking.user_id == current_user.id,
                Booking.idempotency_key == payload.idempotency_key,
            )
            .options(selectinload(Booking.items).selectinload(BookingItem.seat))
            .first()
        )
        if existing_booking:
            # Validate matching payload characteristics
            same_flight = existing_booking.flight_id == payload.flight_id
            same_count = len(existing_booking.items) == len(payload.items)
            
            if same_flight and same_count:
                # Compare class and fare types
                existing_items_sig = sorted(
                    [(it.class_type, it.fare_type, it.passenger_name) for it in existing_booking.items]
                )
                payload_items_sig = sorted(
                    [(it.class_type, it.fare_type, it.passenger_name or current_user.name) for it in payload.items]
                )
                if existing_items_sig == payload_items_sig:
                    return _format_booking_response(existing_booking)

            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key reused with different request parameters",
            )

    # 2. Flight validation
    flight = db.query(Flight).filter(Flight.id == payload.flight_id).first()
    if not flight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Flight '{payload.flight_id}' not found",
        )

    if flight.status in (FlightStatus.CANCELLED, FlightStatus.COMPLETED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Flight status is {flight.status.value}; cannot reserve seats",
        )

    if _ensure_utc(flight.departure_at) <= now_utc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Flight has already departed",
        )


    # 3. Authoritative Fare Rule Lookup & Validation
    resolved_items: List[Tuple[BookingItemHoldRequest, FareRule]] = []
    for item in payload.items:
        fare_rule = (
            db.query(FareRule)
            .filter(
                FareRule.flight_id == payload.flight_id,
                FareRule.class_type == item.class_type,
                FareRule.fare_type == item.fare_type,
            )
            .first()
        )
        if not fare_rule:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No fare rule configured for class '{item.class_type.value}' and fare type '{item.fare_type.value}'",
            )

        # Enforce seat selection rules from authoritative fare rule
        if item.seat_id is not None and not fare_rule.seat_selection_allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat selection is not permitted for fare type '{item.fare_type.value}'. Leave seat_id omitted for automatic assignment.",
            )

        resolved_items.append((item, fare_rule))

    # 4. Seat Allocation and Deterministic Row Locking
    hold_duration = payload.hold_duration_minutes or 10
    hold_expires_at = now_utc + timedelta(minutes=hold_duration)

    allocated_seats: List[Tuple[BookingItemHoldRequest, FareRule, FlightSeat, bool]] = []
    already_assigned_seat_ids = set()

    # Pass 4a: Process explicit seat requests with deterministic lock ordering
    explicit_requests = [r for r in resolved_items if r[0].seat_id is not None]
    explicit_requests_sorted = sorted(explicit_requests, key=lambda r: str(r[0].seat_id))

    for item, fare_rule in explicit_requests_sorted:
        assert item.seat_id is not None
        if item.seat_id in already_assigned_seat_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate seat requested in booking payload: {item.seat_id}",
            )

        # Lock physical seat row
        seat = (
            db.query(FlightSeat)
            .filter(FlightSeat.id == item.seat_id)
            .with_for_update()
            .first()
        )
        if not seat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Seat '{item.seat_id}' not found",
            )

        if seat.flight_id != payload.flight_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat '{item.seat_id}' does not belong to flight '{payload.flight_id}'",
            )

        if seat.class_type != item.class_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat '{seat.seat_number}' belongs to class '{seat.class_type.value}', not requested class '{item.class_type.value}'",
            )

        was_available = seat.status == SeatStatus.AVAILABLE
        is_reclaim = (
            seat.status == SeatStatus.HELD
            and seat.hold_expires_at is not None
            and _ensure_utc(seat.hold_expires_at) <= now_utc
        )

        if not (was_available or is_reclaim):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Seat '{seat.seat_number}' is currently held or unavailable (status: {seat.status.value})",
            )

        # Atomic conditional update on the physical seat row
        updated_count = (
            db.query(FlightSeat)
            .filter(
                FlightSeat.id == seat.id,
                (FlightSeat.status == SeatStatus.AVAILABLE)
                | (
                    (FlightSeat.status == SeatStatus.HELD)
                    & (FlightSeat.hold_expires_at <= now_utc)
                ),
            )
            .update(
                {
                    FlightSeat.status: SeatStatus.HELD,
                    FlightSeat.hold_expires_at: hold_expires_at,
                },
                synchronize_session="fetch",
            )
        )
        if updated_count == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Seat '{seat.seat_number}' is currently held by another user",
            )

        is_new_decrement = was_available
        already_assigned_seat_ids.add(seat.id)
        allocated_seats.append((item, fare_rule, seat, is_new_decrement))

    # Pass 4b: Process automatic seat assignment requests
    auto_requests = [r for r in resolved_items if r[0].seat_id is None]
    for item, fare_rule in auto_requests:
        candidate_seats = (
            db.query(FlightSeat)
            .filter(
                FlightSeat.flight_id == payload.flight_id,
                FlightSeat.class_type == item.class_type,
            )
            .order_by(FlightSeat.seat_number.asc())
            .with_for_update()
            .all()
        )

        chosen_seat = None
        is_new_decrement = False
        for s in candidate_seats:
            if s.id in already_assigned_seat_ids:
                continue

            s_available = s.status == SeatStatus.AVAILABLE
            s_reclaim = (
                s.status == SeatStatus.HELD
                and s.hold_expires_at is not None
                and _ensure_utc(s.hold_expires_at) <= now_utc
            )
            if not (s_available or s_reclaim):
                continue

            # Atomic conditional update
            updated_count = (
                db.query(FlightSeat)
                .filter(
                    FlightSeat.id == s.id,
                    (FlightSeat.status == SeatStatus.AVAILABLE)
                    | (
                        (FlightSeat.status == SeatStatus.HELD)
                        & (FlightSeat.hold_expires_at <= now_utc)
                    ),
                )
                .update(
                    {
                        FlightSeat.status: SeatStatus.HELD,
                        FlightSeat.hold_expires_at: hold_expires_at,
                    },
                    synchronize_session="fetch",
                )
            )
            if updated_count > 0:
                chosen_seat = s
                is_new_decrement = s_available
                break

        if not chosen_seat:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"No available seats remaining in '{item.class_type.value}' class",
            )

        already_assigned_seat_ids.add(chosen_seat.id)
        allocated_seats.append((item, fare_rule, chosen_seat, is_new_decrement))

    # 5. Class Inventory Decrement (Row Locking)
    decrements_per_class: dict[FlightClassType, int] = {}
    for req_item, fare_rule, seat_obj, is_new_decrement in allocated_seats:
        if is_new_decrement:
            cls = req_item.class_type
            decrements_per_class[cls] = decrements_per_class.get(cls, 0) + 1

    # Deterministically lock FlightClass rows sorted by class_type
    for cls_type in sorted(decrements_per_class.keys(), key=lambda c: c.value):
        qty = decrements_per_class[cls_type]
        flight_class = (
            db.query(FlightClass)
            .filter(
                FlightClass.flight_id == payload.flight_id,
                FlightClass.class_type == cls_type,
            )
            .with_for_update()
            .first()
        )
        if not flight_class or flight_class.available_seats < qty:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Insufficient available seats in '{cls_type.value}' class",
            )
        flight_class.available_seats -= qty



    # 7. Create Booking and BookingItems
    total_amount = sum((fare_rule.price for _, fare_rule, _, _ in allocated_seats), Decimal("0.00"))
    currency = allocated_seats[0][1].currency if allocated_seats else "USD"
    booking_ref = _generate_booking_reference(db)

    booking = Booking(
        booking_reference=booking_ref,
        user_id=current_user.id,
        flight_id=payload.flight_id,
        status=BookingStatus.PENDING,
        total_amount=total_amount,
        currency=currency,
        idempotency_key=payload.idempotency_key,
        hold_expires_at=hold_expires_at,
    )
    db.add(booking)
    db.flush()  # populate booking.id

    for item, fare_rule, seat, _ in allocated_seats:
        booking_item = BookingItem(
            booking_id=booking.id,
            flight_id=payload.flight_id,
            seat_id=seat.id,
            class_type=item.class_type,
            fare_type=item.fare_type,
            passenger_name=item.passenger_name or current_user.name,
            price=fare_rule.price,
            currency=fare_rule.currency,
        )
        db.add(booking_item)

    booking_id = booking.id
    db.commit()

    # Re-fetch with relationships loaded
    full_booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .options(selectinload(Booking.items).selectinload(BookingItem.seat))
        .first()
    )
    assert full_booking is not None
    return _format_booking_response(full_booking)



def confirm_booking(
    db: Session,
    booking_id: UUID,
    current_user: User,
) -> BookingResponse:
    """
    Confirms a PENDING booking.
    
    Guarantees:
    - Verifies ownership (passengers only confirm their own bookings).
    - Checks hold expiration: if expired, marks EXPIRED, releases seats, restores available_seats.
    - Transitions seats from HELD -> BOOKED.
    - Does NOT decrement available_seats again (preserves inventory invariant).
    - Idempotent: confirming an already CONFIRMED booking safely returns the booking.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Lock Booking row
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .options(selectinload(Booking.items).selectinload(BookingItem.seat))
        .with_for_update()
        .first()
    )
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking '{booking_id}' not found",
        )

    # 2. Ownership verification
    if current_user.role == UserRole.PASSENGER and booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to confirm this booking",
        )

    # 3. Idempotent check
    if booking.status == BookingStatus.CONFIRMED:
        return _format_booking_response(booking)

    if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot confirm booking with status '{booking.status.value}'",
        )

    # 4. Check Hold Expiration
    if booking.hold_expires_at and _ensure_utc(booking.hold_expires_at) <= now_utc:
        # Reclaim/Release seats back to available
        booking.status = BookingStatus.EXPIRED
        for item in booking.items:
            seat = (
                db.query(FlightSeat)
                .filter(FlightSeat.id == item.seat_id)
                .with_for_update()
                .first()
            )
            if seat and seat.status == SeatStatus.HELD:
                seat.status = SeatStatus.AVAILABLE
                seat.hold_expires_at = None
                
                flight_class = (
                    db.query(FlightClass)
                    .filter(
                        FlightClass.flight_id == booking.flight_id,
                        FlightClass.class_type == item.class_type,
                    )
                    .with_for_update()
                    .first()
                )
                if flight_class:
                    flight_class.available_seats = min(
                        flight_class.total_seats, flight_class.available_seats + 1
                    )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hold has expired. Seat reservation has been released.",
        )

    # 5. Lock seats and verify still HELD
    seat_ids_sorted = sorted([item.seat_id for item in booking.items], key=lambda sid: str(sid))
    seats = (
        db.query(FlightSeat)
        .filter(FlightSeat.id.in_(seat_ids_sorted))
        .with_for_update()
        .all()
    )
    for seat in seats:
        if seat.status != SeatStatus.HELD:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat '{seat.seat_number}' is not in HELD state",
            )
        if seat.hold_expires_at and _ensure_utc(seat.hold_expires_at) <= now_utc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Seat '{seat.seat_number}' hold has expired",
            )
        # Transition HELD -> BOOKED

        seat.status = SeatStatus.BOOKED
        seat.hold_expires_at = None

    # 6. Transition Booking to CONFIRMED
    booking.status = BookingStatus.CONFIRMED
    booking.hold_expires_at = None
    # CRITICAL: Do NOT decrement available_seats again! It was already decremented at hold time.

    db.commit()
    db.refresh(booking)
    return _format_booking_response(booking)


def cancel_booking(
    db: Session,
    booking_id: UUID,
    current_user: User,
) -> BookingResponse:
    """
    Cancels a PENDING or CONFIRMED booking and safely returns reserved seats to AVAILABLE inventory.
    """
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .options(selectinload(Booking.items).selectinload(BookingItem.seat))
        .with_for_update()
        .first()
    )
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking '{booking_id}' not found",
        )

    # Ownership check
    if current_user.role == UserRole.PASSENGER and booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to cancel this booking",
        )

    if booking.status == BookingStatus.CANCELLED:
        return _format_booking_response(booking)

    # Release seats and restore available_seats inventory
    for item in booking.items:
        seat = (
            db.query(FlightSeat)
            .filter(FlightSeat.id == item.seat_id)
            .with_for_update()
            .first()
        )
        if seat and seat.status in (SeatStatus.HELD, SeatStatus.BOOKED):
            seat.status = SeatStatus.AVAILABLE
            seat.hold_expires_at = None

            flight_class = (
                db.query(FlightClass)
                .filter(
                    FlightClass.flight_id == booking.flight_id,
                    FlightClass.class_type == item.class_type,
                )
                .with_for_update()
                .first()
            )
            if flight_class:
                flight_class.available_seats = min(
                    flight_class.total_seats, flight_class.available_seats + 1
                )

    booking.status = BookingStatus.CANCELLED
    booking.hold_expires_at = None

    db.commit()
    db.refresh(booking)
    return _format_booking_response(booking)


def get_booking(
    db: Session,
    booking_id: UUID,
    current_user: User,
) -> BookingResponse:
    """Retrieves single booking details with RBAC enforcement."""
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .options(selectinload(Booking.items).selectinload(BookingItem.seat))
        .first()
    )
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking '{booking_id}' not found",
        )

    if current_user.role == UserRole.PASSENGER and booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this booking",
        )

    return _format_booking_response(booking)


def list_user_bookings(
    db: Session,
    current_user: User,
    page: int = 1,
    page_size: int = 20,
    status_filter: Optional[BookingStatus] = None,
) -> Tuple[List[BookingResponse], BookingPagination]:
    """Lists bookings with pagination and passenger isolation."""
    query = db.query(Booking).options(selectinload(Booking.items).selectinload(BookingItem.seat))

    # Passengers can only see their own bookings; OPS/ADMIN see all
    if current_user.role == UserRole.PASSENGER:
        query = query.filter(Booking.user_id == current_user.id)

    if status_filter:
        query = query.filter(Booking.status == status_filter)

    total = query.count()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    bookings = (
        query.order_by(Booking.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [_format_booking_response(b) for b in bookings]
    pagination = BookingPagination(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )
    return items, pagination
