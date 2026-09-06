from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingItem, Refund
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    RefundReason,
    RefundStatus,
    RefundType,
    SeatStatus,
    UserRole,
)
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.models.user import User
from app.schemas.refund import (
    BookingCancellationRequest,
    CancellationSummaryResponse,
    RefundResponse,
)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Helper to ensure datetime is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cancel_booking_or_items(
    db: Session,
    booking_id: UUID,
    payload: BookingCancellationRequest,
    current_user: User,
) -> CancellationSummaryResponse:
    """
    Cancels an entire booking or specific items (partial cancellation), calculates fare-rule-based
    refunds with strict Decimal precision, restores physical seat & class inventory atomically,
    and logs audit records.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Idempotency Check
    if payload.idempotency_key:
        existing_refunds = (
            db.query(Refund)
            .filter(
                Refund.booking_id == booking_id,
                Refund.idempotency_key == payload.idempotency_key,
            )
            .all()
        )
        if existing_refunds:
            booking = db.query(Booking).filter(Booking.id == booking_id).first()
            if booking:
                cancelled_count = len([it for it in booking.items if it.status == BookingItemStatus.CANCELLED])
                remaining_count = len([it for it in booking.items if it.status == BookingItemStatus.CONFIRMED])
                total_refund = sum((r.amount for r in existing_refunds), Decimal("0.00"))
                return CancellationSummaryResponse(
                    booking_id=booking.id,
                    status=booking.status,
                    cancelled_items_count=cancelled_count,
                    remaining_items_count=remaining_count,
                    refunds=[RefundResponse.model_validate(r) for r in existing_refunds],
                    total_refund_amount=total_refund,
                    currency=booking.currency,
                    message="Idempotent replay: cancellation already processed.",
                )

    # 2. Lock Booking Row
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

    # 3. Ownership Verification
    if current_user.role == UserRole.PASSENGER and booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to cancel this booking",
        )

    # 4. Status Check
    if booking.status == BookingStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Booking is already cancelled",
        )

    if booking.status not in (BookingStatus.CONFIRMED, BookingStatus.PENDING):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel booking with status '{booking.status.value}'",
        )

    # 5. Identify Items to Cancel
    target_items: List[BookingItem] = []
    if payload.item_ids:
        # Partial cancellation
        item_ids_set = set(payload.item_ids)
        target_items = [
            it for it in booking.items
            if it.id in item_ids_set and it.status == BookingItemStatus.CONFIRMED
        ]
        if not target_items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="None of the specified booking items are active and confirmed for cancellation",
            )
    else:
        # Full cancellation: atomic conditional update on Booking row to guarantee race-condition safety
        updated_count = (
            db.query(Booking)
            .filter(
                Booking.id == booking_id,
                Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING]),
            )
            .update(
                {
                    Booking.status: BookingStatus.CANCELLED,
                    Booking.hold_expires_at: None,
                },
                synchronize_session=False,
            )
        )
        if updated_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Booking is already cancelled",
            )
        booking.status = BookingStatus.CANCELLED
        booking.hold_expires_at = None

        target_items = [it for it in booking.items if it.status == BookingItemStatus.CONFIRMED]
        if not target_items:
            # If PENDING with no CONFIRMED items, cancel all items
            target_items = list(booking.items)

    # 6. Authoritative Flight & Fare Rules Lookup
    flight = db.query(Flight).filter(Flight.id == booking.flight_id).first()
    assert flight is not None

    created_refunds: List[Refund] = []
    total_refund_amount = Decimal("0.00")

    # Sort items deterministically by seat_id to avoid lock ordering deadlocks
    target_items_sorted = sorted(target_items, key=lambda it: str(it.seat_id))

    for item in target_items_sorted:
        # Prevent duplicate active refund on item level
        existing_item_refund = (
            db.query(Refund)
            .filter(
                Refund.booking_item_id == item.id,
                Refund.status.in_([RefundStatus.PENDING, RefundStatus.APPROVED, RefundStatus.PROCESSING, RefundStatus.COMPLETED]),
            )
            .first()
        )
        if existing_item_refund:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A refund has already been recorded for item '{item.id}'",
            )

        # Lookup authoritative fare rule snapshot (prefer immutable snapshot saved at purchase time)
        if item.fare_rule_snapshot and isinstance(item.fare_rule_snapshot, dict):
            cancellation_cutoff = int(item.fare_rule_snapshot.get("cancellation_cutoff_minutes", 0))
            is_refundable = bool(item.fare_rule_snapshot.get("refundable", False))
            is_credit_only = bool(item.fare_rule_snapshot.get("credit_only", False))
        else:
            fare_rule = (
                db.query(FareRule)
                .filter(
                    FareRule.flight_id == booking.flight_id,
                    FareRule.class_type == item.class_type,
                    FareRule.fare_type == item.fare_type,
                )
                .first()
            )
            cancellation_cutoff = fare_rule.cancellation_cutoff_minutes if fare_rule else 0
            is_refundable = fare_rule.refundable if fare_rule else False
            is_credit_only = fare_rule.credit_only if fare_rule else False

        # Cancellation Cutoff Evaluation
        cutoff_deadline = _ensure_utc(flight.departure_at) - timedelta(minutes=cancellation_cutoff)
        is_before_cutoff = now_utc <= cutoff_deadline

        # Refund Policy Application
        refund_amount = Decimal("0.00")
        refund_type = RefundType.NONE
        notes = "Cancelled"

        if not is_before_cutoff:
            refund_type = RefundType.NONE
            refund_amount = Decimal("0.00")
            notes = f"Cancellation request after deadline ({cutoff_deadline.isoformat()})"
        elif is_refundable:
            refund_type = RefundType.MONETARY
            refund_amount = item.price
            notes = "Eligible for monetary refund per fare rules"
        elif is_credit_only:
            refund_type = RefundType.CREDIT
            refund_amount = item.price
            notes = "Eligible for credit-only refund per fare rules"
        else:
            refund_type = RefundType.NONE
            refund_amount = Decimal("0.00")
            notes = "Non-refundable fare per fare rules"

        # Update item status atomically
        if payload.item_ids:
            updated_item = (
                db.query(BookingItem)
                .filter(
                    BookingItem.id == item.id,
                    BookingItem.status == BookingItemStatus.CONFIRMED,
                )
                .update(
                    {BookingItem.status: BookingItemStatus.CANCELLED},
                    synchronize_session=False,
                )
            )
            if updated_item == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Booking item '{item.id}' is already cancelled or not active",
                )
            item.status = BookingItemStatus.CANCELLED
        else:
            item.status = BookingItemStatus.CANCELLED
            db.query(BookingItem).filter(BookingItem.id == item.id).update(
                {BookingItem.status: BookingItemStatus.CANCELLED},
                synchronize_session=False,
            )

        # 7. Atomic Seat and Class Inventory Restoration
        seat_updated = (
            db.query(FlightSeat)
            .filter(
                FlightSeat.id == item.seat_id,
                FlightSeat.status.in_([SeatStatus.BOOKED, SeatStatus.HELD]),
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
            # Increment class available_seats (capped at total_seats)
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

        # 8. Create Dedicated Refund Record
        refund = Refund(
            booking_id=booking.id,
            booking_item_id=item.id,
            amount=refund_amount,
            currency=item.currency,
            refund_type=refund_type,
            status=RefundStatus.PENDING if refund_type != RefundType.NONE else RefundStatus.COMPLETED,
            reason=payload.reason or RefundReason.CUSTOMER_CANCELLATION,
            idempotency_key=payload.idempotency_key,
            notes=notes,
        )
        db.add(refund)
        created_refunds.append(refund)
        total_refund_amount += refund_amount

    # 9. Update Booking Overall Status for Partial Cancellations
    if payload.item_ids:
        remaining_count = (
            db.query(BookingItem)
            .filter(
                BookingItem.booking_id == booking.id,
                BookingItem.status == BookingItemStatus.CONFIRMED,
            )
            .count()
        )
        if remaining_count == 0:
            booking.status = BookingStatus.CANCELLED
            booking.hold_expires_at = None
            db.query(Booking).filter(Booking.id == booking.id).update(
                {
                    Booking.status: BookingStatus.CANCELLED,
                    Booking.hold_expires_at: None,
                },
                synchronize_session=False,
            )
            remaining_active_count = 0
        else:
            remaining_active_count = remaining_count
    else:
        remaining_active_count = 0

    # 10. Audit Logging
    audit = AuditLog(
        user_id=current_user.id,
        action="CANCEL_BOOKING" if booking.status == BookingStatus.CANCELLED else "PARTIAL_CANCEL_BOOKING",
        entity_type="BOOKING",
        entity_id=booking.id,
        old_values={"status": BookingStatus.CONFIRMED.value},
        new_values={
            "status": booking.status.value,
            "cancelled_items_count": len(target_items),
            "total_refund_amount": str(total_refund_amount),
            "currency": booking.currency,
        },
    )
    db.add(audit)

    db.commit()

    # Re-fetch refunds for response serialization
    db_refunds = db.query(Refund).filter(Refund.id.in_([r.id for r in created_refunds])).all()

    return CancellationSummaryResponse(
        booking_id=booking.id,
        status=booking.status,
        cancelled_items_count=len(target_items),
        remaining_items_count=remaining_active_count,
        refunds=[RefundResponse.model_validate(r) for r in db_refunds],
        total_refund_amount=total_refund_amount,
        currency=booking.currency,
        message=(
            "Booking cancelled successfully."
            if booking.status == BookingStatus.CANCELLED
            else "Booking items cancelled successfully. Remaining items remain confirmed."
        ),
    )


def list_booking_refunds(
    db: Session,
    booking_id: UUID,
    current_user: User,
) -> List[RefundResponse]:
    """Retrieves all refund records for a booking with RBAC checks."""
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking '{booking_id}' not found",
        )

    if current_user.role == UserRole.PASSENGER and booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view refunds for this booking",
        )

    refunds = (
        db.query(Refund)
        .filter(Refund.booking_id == booking_id)
        .order_by(Refund.created_at.asc())
        .all()
    )
    return [RefundResponse.model_validate(r) for r in refunds]
