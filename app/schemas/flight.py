from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import FlightClassType, FlightStatus, SeatStatus


class FlightClassResponse(BaseModel):
    id: UUID
    flight_id: UUID
    class_type: FlightClassType
    total_seats: int
    available_seats: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FlightSeatResponse(BaseModel):
    id: UUID
    flight_id: UUID
    seat_number: str
    class_type: FlightClassType
    status: SeatStatus
    hold_expires_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class FlightCreate(BaseModel):
    flight_number: str = Field(..., min_length=2, max_length=20, examples=["UK123"])
    origin: str = Field(..., min_length=3, max_length=10, examples=["LHR"])
    destination: str = Field(..., min_length=3, max_length=10, examples=["DXB"])
    departure_at: datetime = Field(..., examples=["2026-10-15T10:00:00Z"])
    arrival_at: datetime = Field(..., examples=["2026-10-15T18:00:00Z"])
    total_capacity: int = Field(..., gt=0, examples=[100])
    first_class_seats: int = Field(..., gt=0, examples=[20])
    business_class_seats: int = Field(..., gt=0, examples=[30])
    economy_class_seats: int = Field(..., gt=0, examples=[50])

    @model_validator(mode="after")
    def validate_flight_fields(self) -> "FlightCreate":
        # Normalize strings
        self.flight_number = self.flight_number.strip().upper()
        self.origin = self.origin.strip().upper()
        self.destination = self.destination.strip().upper()

        if not self.flight_number:
            raise ValueError("flight_number cannot be empty or whitespace only")

        if not self.origin or not self.destination:
            raise ValueError("origin and destination cannot be empty")

        if self.origin == self.destination:
            raise ValueError(f"Origin and destination cannot be identical ({self.origin})")

        if self.arrival_at <= self.departure_at:
            raise ValueError("arrival_at must be strictly later than departure_at")

        # Invariant: FIRST + BUSINESS + ECONOMY == total_capacity
        class_seats_sum = self.first_class_seats + self.business_class_seats + self.economy_class_seats
        if class_seats_sum != self.total_capacity:
            raise ValueError(
                f"Class capacity mismatch: First ({self.first_class_seats}) + "
                f"Business ({self.business_class_seats}) + Economy ({self.economy_class_seats}) = {class_seats_sum}, "
                f"which does not match total_capacity ({self.total_capacity})"
            )

        return self


class FlightUpdate(BaseModel):
    flight_number: Optional[str] = Field(None, min_length=2, max_length=20)
    origin: Optional[str] = Field(None, min_length=3, max_length=10)
    destination: Optional[str] = Field(None, min_length=3, max_length=10)
    departure_at: Optional[datetime] = None
    arrival_at: Optional[datetime] = None
    status: Optional[FlightStatus] = None

    @model_validator(mode="after")
    def validate_update(self) -> "FlightUpdate":
        if self.flight_number is not None:
            self.flight_number = self.flight_number.strip().upper()
            if not self.flight_number:
                raise ValueError("flight_number cannot be empty")

        if self.origin is not None:
            self.origin = self.origin.strip().upper()
            if not self.origin:
                raise ValueError("origin cannot be empty")

        if self.destination is not None:
            self.destination = self.destination.strip().upper()
            if not self.destination:
                raise ValueError("destination cannot be empty")

        if self.origin is not None and self.destination is not None:
            if self.origin == self.destination:
                raise ValueError("Origin and destination cannot be identical")

        if self.departure_at is not None and self.arrival_at is not None:
            if self.arrival_at <= self.departure_at:
                raise ValueError("arrival_at must be strictly later than departure_at")

        return self


class FlightResponse(BaseModel):
    id: UUID
    flight_number: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    total_capacity: int
    status: FlightStatus
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime
    classes: List[FlightClassResponse] = []

    model_config = ConfigDict(from_attributes=True)


class FlightDetailResponse(FlightResponse):
    total_seats_count: int = 0
    available_seats_count: int = 0
    seats: Optional[List[FlightSeatResponse]] = None


class FlightListResponse(BaseModel):
    items: List[FlightResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
