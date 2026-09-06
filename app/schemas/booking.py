from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BookingStatus, FareType, FlightClassType


class BookingItemHoldRequest(BaseModel):
    seat_id: Optional[UUID] = Field(None, description="Physical seat ID to hold (if omitted, auto-assigned)")
    class_type: FlightClassType = Field(..., description="Cabin class (FIRST, BUSINESS, ECONOMY)")
    fare_type: FareType = Field(..., description="Fare rule type (BASIC, FLEXIBLE)")
    passenger_name: Optional[str] = Field(None, min_length=1, max_length=255, description="Passenger full name")


class BookingHoldCreate(BaseModel):
    flight_id: UUID = Field(..., description="Target flight UUID")
    items: List[BookingItemHoldRequest] = Field(..., min_length=1, description="List of passenger items/seats to hold")
    idempotency_key: Optional[str] = Field(None, max_length=255, description="Client idempotency key")
    hold_duration_minutes: Optional[int] = Field(10, ge=1, le=60, description="Hold duration in minutes (default 10)")


class BookingItemResponse(BaseModel):
    id: UUID
    booking_id: UUID
    flight_id: UUID
    seat_id: UUID
    seat_number: Optional[str] = None
    class_type: FlightClassType
    fare_type: FareType
    passenger_name: str
    price: Decimal
    currency: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookingResponse(BaseModel):
    id: UUID
    booking_reference: str
    user_id: UUID
    flight_id: UUID
    status: BookingStatus
    total_amount: Decimal
    currency: str
    idempotency_key: Optional[str] = None
    hold_expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    items: List[BookingItemResponse] = []

    model_config = ConfigDict(from_attributes=True)


class BookingPagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class BookingListResponse(BaseModel):
    items: List[BookingResponse]
    pagination: BookingPagination
