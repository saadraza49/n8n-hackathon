from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.enums import BookingStatus
from app.models.user import User
from app.schemas.booking import (
    BookingHoldCreate,
    BookingListResponse,
    BookingResponse,
)
from app.schemas.booking_change import BookingChangeRequest, BookingChangeResponse
from app.schemas.refund import (
    BookingCancellationRequest,
    CancellationSummaryResponse,
    RefundResponse,
)
from app.services.booking_change_service import request_booking_change
from app.services.booking_service import (
    confirm_booking,
    create_hold,
    get_booking,
    list_user_bookings,
)
from app.services.cancellation_service import (
    cancel_booking_or_items,
    list_booking_refunds,
)

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.post(
    "/holds",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Temporary Seat Hold",
    description="Reserve physical seats temporarily with atomic inventory decrement, row locking, and idempotency.",
)
def create_seat_hold(
    payload: BookingHoldCreate,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if idempotency_key_header and not payload.idempotency_key:
        payload.idempotency_key = idempotency_key_header

    return create_hold(db=db, payload=payload, current_user=current_user)


@router.post(
    "/{booking_id}/confirm",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm Booking",
    description="Confirm a pending booking hold, transitioning seats from HELD to BOOKED without double-decrementing inventory.",
)
def confirm_seat_booking(
    booking_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return confirm_booking(db=db, booking_id=booking_id, current_user=current_user)


@router.post(
    "/{booking_id}/cancel",
    response_model=CancellationSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel Booking or Items",
    description="Cancel an entire booking or specific passenger items, restoring seat/class inventory and calculating fare-rule-based refunds.",
)
def cancel_seat_booking(
    booking_id: UUID,
    payload: Optional[BookingCancellationRequest] = None,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload is None:
        payload = BookingCancellationRequest()
    if idempotency_key_header and not payload.idempotency_key:
        payload.idempotency_key = idempotency_key_header

    return cancel_booking_or_items(
        db=db,
        booking_id=booking_id,
        payload=payload,
        current_user=current_user,
    )


@router.get(
    "/{booking_id}/refunds",
    response_model=List[RefundResponse],
    status_code=status.HTTP_200_OK,
    summary="List Booking Refunds",
    description="Retrieve all refund records associated with a booking (monetary, credit, or none).",
)
def get_booking_refunds(
    booking_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return list_booking_refunds(
        db=db,
        booking_id=booking_id,
        current_user=current_user,
    )


@router.post(
    "/{booking_id}/change",
    response_model=BookingChangeResponse,
    status_code=status.HTTP_200_OK,
    summary="Change / Rebook Booking Item",
    description="Rebook a booking item to another flight/class/fare, swapping seats atomically and calculating price differences.",
)
def change_booking(
    booking_id: UUID,
    payload: BookingChangeRequest,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if idempotency_key_header and not payload.idempotency_key:
        payload.idempotency_key = idempotency_key_header

    return request_booking_change(
        db=db,
        booking_id=booking_id,
        payload=payload,
        current_user=current_user,
    )


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Booking Details",
    description="Retrieve full booking details by ID with RBAC isolation (passengers only access their own).",
)
def retrieve_booking(
    booking_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_booking(db=db, booking_id=booking_id, current_user=current_user)


@router.get(
    "",
    response_model=BookingListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Bookings",
    description="List paginated bookings. Normal passengers only see their own bookings; ops and admin see system-wide bookings.",
)
def list_bookings(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=50, description="Items per page"),
    status: Optional[BookingStatus] = Query(None, description="Optional booking status filter"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, pagination = list_user_bookings(
        db=db,
        current_user=current_user,
        page=page,
        page_size=page_size,
        status_filter=status,
    )
    return BookingListResponse(items=items, pagination=pagination)
