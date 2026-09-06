from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_ops_or_admin, get_current_user
from app.models.enums import FlightClassType, UserRole
from app.models.user import User
from app.schemas.waitlist import (
    WaitlistClaimRequest,
    WaitlistClaimResponse,
    WaitlistEntryResponse,
    WaitlistFlightQueueResponse,
    WaitlistJoinRequest,
    WaitlistPromoteRequest,
)
from app.services.waitlist_service import (
    cancel_waitlist_entry,
    claim_promoted_seat,
    get_flight_waitlist_queue,
    get_passenger_waitlists,
    get_waitlist_entry_by_id,
    join_waitlist,
    promote_next_passenger,
)

router = APIRouter(tags=["Waitlist & Standby"])


# ============================================================================
# 1. FLIGHT-LEVEL WAITLIST ENDPOINTS
# ============================================================================

@router.post(
    "/flights/{flight_id}/waitlist",
    response_model=WaitlistEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Join Waitlist",
    description="Join a class-specific waitlist when the cabin class is fully booked (available_seats = 0).",
)
def join_flight_waitlist(
    flight_id: UUID,
    payload: WaitlistJoinRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if idempotency_key and not payload.idempotency_key:
        payload.idempotency_key = idempotency_key
    return join_waitlist(db=db, flight_id=flight_id, payload=payload, current_user=current_user)


@router.get(
    "/flights/{flight_id}/waitlist",
    response_model=WaitlistFlightQueueResponse,
    summary="Inspect Flight Waitlist Queue",
    description="Operations Agents & Super Admins inspect waitlist queue with deterministic priority ordering.",
)
def inspect_flight_waitlist_queue(
    flight_id: UUID,
    class_type: Optional[FlightClassType] = Query(None, description="Optional filter by cabin class"),
    current_user: User = Depends(get_current_ops_or_admin),
    db: Session = Depends(get_db),
):
    return get_flight_waitlist_queue(db=db, flight_id=flight_id, class_type=class_type)


@router.post(
    "/flights/{flight_id}/waitlist/promote",
    response_model=Optional[WaitlistEntryResponse],
    summary="Promote Next Waitlisted Passenger",
    description="Operational endpoint to promote the highest priority WAITING passenger when inventory is available.",
)
def promote_waitlist_passenger(
    flight_id: UUID,
    payload: WaitlistPromoteRequest,
    current_user: User = Depends(get_current_ops_or_admin),
    db: Session = Depends(get_db),
):
    return promote_next_passenger(
        db=db,
        flight_id=flight_id,
        class_type=payload.class_type,
        claim_window_minutes=payload.claim_window_minutes or 15,
        current_user=current_user,
    )


# ============================================================================
# 2. PASSENGER & ENTRY-LEVEL WAITLIST ENDPOINTS
# ============================================================================

@router.get(
    "/waitlists/me",
    response_model=List[WaitlistEntryResponse],
    summary="List My Waitlist Entries",
    description="Retrieve all waitlist entries for the authenticated passenger.",
)
def list_my_waitlist_entries(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_passenger_waitlists(db=db, passenger_id=current_user.id)


@router.get(
    "/me/waitlists",
    response_model=List[WaitlistEntryResponse],
    summary="List My Waitlist Entries (Alias)",
    description="Alternative path for passenger waitlist entries list.",
    include_in_schema=False,
)
def list_my_waitlists_alias(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_passenger_waitlists(db=db, passenger_id=current_user.id)


@router.get(
    "/waitlists/{waitlist_id}",
    response_model=WaitlistEntryResponse,
    summary="Get Waitlist Entry Details",
    description="Retrieve specific waitlist entry details. Passengers can only view their own.",
)
def get_waitlist_entry(
    waitlist_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_waitlist_entry_by_id(db=db, waitlist_id=waitlist_id, current_user=current_user)


@router.delete(
    "/waitlists/{waitlist_id}",
    response_model=WaitlistEntryResponse,
    summary="Cancel Waitlist Entry",
    description="Cancel an active waitlist entry. If PROMOTED, releases held physical seat and restores inventory.",
)
def delete_waitlist_entry(
    waitlist_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return cancel_waitlist_entry(db=db, waitlist_id=waitlist_id, current_user=current_user)


@router.post(
    "/waitlists/{waitlist_id}/cancel",
    response_model=WaitlistEntryResponse,
    summary="Cancel Waitlist Entry (POST Alias)",
    description="POST alias for cancelling waitlist entry.",
    include_in_schema=False,
)
def cancel_waitlist_entry_post(
    waitlist_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return cancel_waitlist_entry(db=db, waitlist_id=waitlist_id, current_user=current_user)


@router.post(
    "/waitlists/{waitlist_id}/claim",
    response_model=WaitlistClaimResponse,
    summary="Claim Promoted Seat Opportunity",
    description="Promoted passenger claims seat within claim deadline, converting it into a confirmed booking.",
)
def claim_promoted_opportunity(
    waitlist_id: UUID,
    payload: WaitlistClaimRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if idempotency_key and not payload.idempotency_key:
        payload.idempotency_key = idempotency_key
    return claim_promoted_seat(db=db, waitlist_id=waitlist_id, payload=payload, current_user=current_user)
