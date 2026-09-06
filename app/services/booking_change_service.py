from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingChange, BookingItem
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FlightStatus,
    SeatStatus,
    UserRole,
)
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User
from app.schemas.booking_change import BookingChangeRequest, BookingChangeResponse


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Helper to ensure datetime is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def request_booking_change(
    db: Session,
    booking_id: UUID,
    payload: BookingChangeRequest,
    current_user: User,
) -> BookingChangeResponse:
    """
    Executes a rebooking / booking change for a specific item:
    - Validates fare rule allows changes (changes_allowed).
    - Validates target flight and class inventory.
    - Swaps seats atomically (target seat BOOKED, old seat AVAILABLE).
    - Restores and decrements relevant class inventories.
    - Calculates price difference using Decimal.
    - Preserves historical change log in booking_changes.
    - Records audit log.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Idempotency Check
    if payload.idempotency_key:
        existing_change = (
            db.query(BookingChange)
            .filter(
                BookingChange.booking_id == booking_id,
                BookingChange.idempotency_key == payload.idempotency_key,
            )
            .first()
        )
        if existing_change:
            return BookingChangeResponse.model_validate(existing_change)

    # 2. Lock Booking
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .with_for_update()
        .first()
    )
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking '{booking_id}' not found",
        )

    # 3. Ownership Verification
    if current_user.role == UserRole.PASSENGER and booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to change this booking",
        )

    # 4. Status Check
    if booking.status != BookingStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot change booking with status '{booking.status.value}'. Must be CONFIRMED.",
        )

    # 5. Lock Target Item
    item = (
        db.query(BookingItem)
        .filter(
            BookingItem.id == payload.item_id,
            BookingItem.booking_id == booking_id,
        )
        .with_for_update()
        .first()
    )
    if not item or item.status != BookingItemStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Booking item is not active or confirmed for change",
        )

    # 6. Check Fare Rule Change Eligibility
    current_fare_rule = (
        db.query(FareRule)
        .filter(
            FareRule.flight_id == item.flight_id,
            FareRule.class_type == item.class_type,
            FareRule.fare_type == item.fare_type,
        )
        .first()
    )
    if not current_fare_rule or not current_fare_rule.changes_allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Changes are not permitted for this fare type per fare rules",
        )

    # 7. Validate Target Flight
    target_flight = db.query(Flight).filter(Flight.id == payload.new_flight_id).first()
    if not target_flight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target flight '{payload.new_flight_id}' not found",
        )

    if target_flight.status in (FlightStatus.CANCELLED, FlightStatus.COMPLETED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Target flight status is '{target_flight.status.value}'; cannot rebook",
        )

    if _ensure_utc(target_flight.departure_at) <= now_utc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target flight has already departed",
        )

    # 8. Validate Target Fare Rule
    target_fare_rule = (
        db.query(FareRule)
        .filter(
            FareRule.flight_id == payload.new_flight_id,
            FareRule.class_type == payload.new_class_type,
            FareRule.fare_type == payload.new_fare_type,
        )
        .first()
    )
    if not target_fare_rule:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No fare rule configured for target class '{payload.new_class_type.value}' and fare '{payload.new_fare_type.value}'",
        )

    # 9. Check Target Class Inventory
    target_class = (
        db.query(FlightClass)
        .filter(
            FlightClass.flight_id == payload.new_flight_id,
            FlightClass.class_type == payload.new_class_type,
        )
        .with_for_update()
        .first()
    )
    if not target_class or target_class.available_seats <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No available seats remaining in target class '{payload.new_class_type.value}'",
        )

    # 10. Allocate Target Seat
    new_seat = None
    if payload.new_seat_id:
        if not target_fare_rule.seat_selection_allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Seat selection is not permitted for target fare type",
            )
        new_seat = (
            db.query(FlightSeat)
            .filter(FlightSeat.id == payload.new_seat_id)
            .with_for_update()
            .first()
        )
        if not new_seat or new_seat.flight_id != payload.new_flight_id or new_seat.class_type != payload.new_class_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Target seat does not belong to specified flight or class",
            )
        if new_seat.status != SeatStatus.AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Target seat '{new_seat.seat_number}' is not available",
            )
    else:
        # Auto-assign available seat in target class
        new_seat = (
            db.query(FlightSeat)
            .filter(
                FlightSeat.flight_id == payload.new_flight_id,
                FlightSeat.class_type == payload.new_class_type,
                FlightSeat.status == SeatStatus.AVAILABLE,
            )
            .order_by(FlightSeat.seat_number.asc())
            .with_for_update()
            .first()
        )
        if not new_seat:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"No available seats remaining in target class '{payload.new_class_type.value}'",
            )

    # 11. Atomic Seat Swap and Inventory Updates
    # Reserve new seat & decrement target class inventory
    new_seat.status = SeatStatus.BOOKED
    new_seat.hold_expires_at = None
    target_class.available_seats -= 1

    # Release old seat & restore old class inventory
    old_seat = (
        db.query(FlightSeat)
        .filter(FlightSeat.id == item.seat_id)
        .with_for_update()
        .first()
    )
    if old_seat:
        old_seat.status = SeatStatus.AVAILABLE
        old_seat.hold_expires_at = None

        old_class = (
            db.query(FlightClass)
            .filter(
                FlightClass.flight_id == item.flight_id,
                FlightClass.class_type == item.class_type,
            )
            .with_for_update()
            .first()
        )
        if old_class:
            old_class.available_seats = min(old_class.total_seats, old_class.available_seats + 1)

    # 12. Price Difference Calculation (Strict Decimal)
    old_price = item.price
    new_price = target_fare_rule.price
    price_diff = new_price - old_price

    # 13. Create Immutable History Record
    change_record = BookingChange(
        booking_id=booking.id,
        booking_item_id=item.id,
        old_flight_id=item.flight_id,
        new_flight_id=payload.new_flight_id,
        old_seat_id=old_seat.id if old_seat else item.seat_id,
        new_seat_id=new_seat.id,
        old_class_type=item.class_type,
        new_class_type=payload.new_class_type,
        old_fare_type=item.fare_type,
        new_fare_type=payload.new_fare_type,
        old_price=old_price,
        new_price=new_price,
        price_difference=price_diff,
        currency=target_fare_rule.currency,
        idempotency_key=payload.idempotency_key,
    )
    db.add(change_record)

    # 14. Update BookingItem with new details
    item.flight_id = payload.new_flight_id
    item.seat_id = new_seat.id
    item.class_type = payload.new_class_type
    item.fare_type = payload.new_fare_type
    item.price = target_fare_rule.price
    item.currency = target_fare_rule.currency

    # Recompute booking total_amount
    booking.total_amount = sum(
        (it.price for it in booking.items if it.status == BookingItemStatus.CONFIRMED),
        Decimal("0.00"),
    )

    # 15. Audit Log
    audit = AuditLog(
        user_id=current_user.id,
        action="CHANGE_BOOKING",
        entity_type="BOOKING",
        entity_id=booking.id,
        old_values={
            "flight_id": str(change_record.old_flight_id),
            "class_type": change_record.old_class_type.value,
            "fare_type": change_record.old_fare_type.value,
            "price": str(old_price),
        },
        new_values={
            "flight_id": str(change_record.new_flight_id),
            "class_type": change_record.new_class_type.value,
            "fare_type": change_record.new_fare_type.value,
            "price": str(new_price),
            "price_difference": str(price_diff),
        },
    )
    db.add(audit)

    db.commit()
    db.refresh(change_record)
    return BookingChangeResponse.model_validate(change_record)
