from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FareType, FlightClassType


class BookingChangeRequest(BaseModel):
    item_id: UUID = Field(..., description="ID of the specific booking item to change/rebook")
    new_flight_id: UUID = Field(..., description="Target flight UUID")
    new_class_type: FlightClassType = Field(..., description="Target cabin class (FIRST, BUSINESS, ECONOMY)")
    new_fare_type: FareType = Field(..., description="Target fare rule type (BASIC, FLEXIBLE)")
    new_seat_id: Optional[UUID] = Field(None, description="Optional specific physical seat ID on target flight")
    idempotency_key: Optional[str] = Field(None, max_length=255, description="Client idempotency key")


class BookingChangeResponse(BaseModel):
    id: UUID
    booking_id: UUID
    booking_item_id: UUID
    old_flight_id: UUID
    new_flight_id: UUID
    old_seat_id: UUID
    new_seat_id: UUID
    old_class_type: FlightClassType
    new_class_type: FlightClassType
    old_fare_type: FareType
    new_fare_type: FareType
    old_price: Decimal
    new_price: Decimal
    price_difference: Decimal
    currency: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
