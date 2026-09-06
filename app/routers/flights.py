from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_ops_or_admin, get_current_user
from app.models.enums import FlightStatus
from app.models.user import User
from app.schemas.booking import BookingResponse
from app.schemas.fare_rule import FareRuleCreate, FareRuleResponse, FareRuleUpdate

from app.schemas.flight import (
    FlightCreate,
    FlightDetailResponse,
    FlightListResponse,
    FlightResponse,
    FlightUpdate,
)
from app.schemas.search import FlightSearchResponse
from app.services.fare_service import FareService
from app.services.flight_service import FlightService
from app.services.search_service import FlightSearchService

router = APIRouter(prefix="/flights", tags=["Flight Management"])


# ==========================================
# 1. FLIGHT CREATION & OPERATIONAL SEARCH
# ==========================================

@router.post(
    "",
    response_model=FlightResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Flight",
    description=(
        "Create a new flight with class inventory, deterministic physical seats, "
        "and audit log in an atomic transaction. Restricted to OPS_AGENT and SUPER_ADMIN."
    ),
)
def create_flight(
    payload: FlightCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return FlightService.create_flight(db=db, payload=payload, actor_id=current_user.id)


@router.get(
    "",
    response_model=FlightListResponse,
    status_code=status.HTTP_200_OK,
    summary="List & Search Flights (Admin/Operational)",
    description="Retrieve paginated list of flights with optional filtering by route, dates, status, or flight number.",
)
def list_flights(
    origin: Optional[str] = Query(None, description="Origin airport code (e.g. LHR)"),
    destination: Optional[str] = Query(None, description="Destination airport code (e.g. DXB)"),
    flight_number: Optional[str] = Query(None, description="Flight number search query"),
    flight_status: Optional[FlightStatus] = Query(None, alias="status", description="Filter by flight status"),
    departure_from: Optional[datetime] = Query(None, description="Filter departure at or after this timestamp"),
    departure_to: Optional[datetime] = Query(None, description="Filter departure at or before this timestamp"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    flights, total, total_pages = FlightService.list_flights(
        db=db,
        origin=origin,
        destination=destination,
        flight_number=flight_number,
        status_filter=flight_status,
        departure_from=departure_from,
        departure_to=departure_to,
        page=page,
        page_size=page_size,
    )
    return FlightListResponse(
        items=flights,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# ==========================================
# 2. PASSENGER FLIGHT & FARE SEARCH
# ==========================================

@router.get(
    "/search",
    response_model=FlightSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Passenger Flight & Fare Search",
    description=(
        "Public and passenger-facing search for bookable scheduled flights by route and departure date. "
        "Returns availability-aware cabin classes, basic/flexible fare tiers, and financial accuracy with zero N+1 queries. "
        "Strictly read-only."
    ),
)
def search_flights(
    origin: str = Query(..., min_length=3, max_length=10, description="Origin airport code (e.g. LHR)"),
    destination: str = Query(..., min_length=3, max_length=10, description="Destination airport code (e.g. DXB)"),
    departure_date: date = Query(..., description="Flight departure date (YYYY-MM-DD)"),
    departure_from: Optional[datetime] = Query(None, description="Optional earliest departure timestamp"),
    departure_to: Optional[datetime] = Query(None, description="Optional latest departure timestamp"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=50, description="Items per page (max 50)"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    return FlightSearchService.search_flights(
        db=db,
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        departure_from=departure_from,
        departure_to=departure_to,
        page=page,
        page_size=page_size,
    )


# ==========================================
# 3. FLIGHT DETAILS & MUTATIONS
# ==========================================

@router.get(
    "/{flight_id}",
    response_model=FlightDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Flight Details",
    description="Retrieve comprehensive details for a flight including class inventories and seat availability stats.",
)
def get_flight(
    flight_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    flight, total_seats, available_seats = FlightService.get_flight_details(db=db, flight_id=flight_id)
    response = FlightDetailResponse.model_validate(flight)
    response.total_seats_count = total_seats
    response.available_seats_count = available_seats
    return response


@router.patch(
    "/{flight_id}",
    response_model=FlightResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Flight",
    description="Update flight details and status with audit logging. Restricted to OPS_AGENT and SUPER_ADMIN.",
)
def update_flight(
    flight_id: UUID,
    payload: FlightUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return FlightService.update_flight(
        db=db, flight_id=flight_id, payload=payload, actor_id=current_user.id
    )


@router.post(
    "/{flight_id}/cancel",
    response_model=FlightResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel Flight",
    description=(
        "Cancel a flight without deleting historical classes or seat records. "
        "Records an audit log entry. Restricted to OPS_AGENT and SUPER_ADMIN."
    ),
)
def cancel_flight(
    flight_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return FlightService.cancel_flight(db=db, flight_id=flight_id, actor_id=current_user.id)


# ==========================================
# 4. FARE RULES MANAGEMENT (CRUD)
# ==========================================

@router.post(
    "/{flight_id}/fares",
    response_model=FareRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Fare Rule",
    description=(
        "Configure a fare rule (pricing and policies) for a specific class on a flight. "
        "Restricted to OPS_AGENT and SUPER_ADMIN."
    ),
)
def create_fare_rule(
    flight_id: UUID,
    payload: FareRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return FareService.create_fare_rule(
        db=db, flight_id=flight_id, payload=payload, actor_id=current_user.id
    )


@router.get(
    "/{flight_id}/fares",
    response_model=List[FareRuleResponse],
    status_code=status.HTTP_200_OK,
    summary="List Fare Rules for Flight",
    description="Retrieve all configured fare rules (Basic & Flexible) for a flight.",
)
def list_fare_rules(
    flight_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return FareService.list_fare_rules(db=db, flight_id=flight_id)


@router.patch(
    "/{flight_id}/fares/{fare_rule_id}",
    response_model=FareRuleResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Fare Rule",
    description="Update pricing or policy rules for an existing fare rule. Restricted to OPS_AGENT and SUPER_ADMIN.",
)
def update_fare_rule(
    flight_id: UUID,
    fare_rule_id: UUID,
    payload: FareRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return FareService.update_fare_rule(
        db=db,
        flight_id=flight_id,
        fare_rule_id=fare_rule_id,
        payload=payload,
        actor_id=current_user.id,
    )


@router.delete(
    "/{flight_id}/fares/{fare_rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Fare Rule",
    description="Remove a configured fare rule and record an audit log. Restricted to OPS_AGENT and SUPER_ADMIN.",
)
def delete_fare_rule(
    flight_id: UUID,
    fare_rule_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    FareService.delete_fare_rule(
        db=db, flight_id=flight_id, fare_rule_id=fare_rule_id, actor_id=current_user.id
    )
    return None


@router.get(
    "/{flight_id}/affected-bookings",
    response_model=List[BookingResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Affected Bookings",
    description="Retrieve all confirmed bookings affected by operational flight cancellation or schedule change. Restricted to OPS_AGENT and SUPER_ADMIN.",
)
def get_affected_flight_bookings(
    flight_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    bookings = FlightService.get_affected_bookings(db=db, flight_id=flight_id)
    from app.services.booking_service import _format_booking_response
    return [_format_booking_response(b) for b in bookings]

