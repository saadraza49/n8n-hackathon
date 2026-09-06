from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FareType, FlightClassType


class FareRuleCreate(BaseModel):
    class_type: FlightClassType = Field(..., description="Flight cabin class: FIRST, BUSINESS, or ECONOMY")
    fare_type: FareType = Field(..., description="Fare tier: BASIC or FLEXIBLE")
    price: Decimal = Field(..., ge=0, decimal_places=2, description="Price in the specified currency")
    currency: str = Field("USD", min_length=3, max_length=3, description="3-letter currency code (e.g. USD, GBP, PKR, AED)")
    changes_allowed: bool = Field(False, description="Whether itinerary changes are permitted")
    seat_selection_allowed: bool = Field(False, description="Whether advance seat selection is included")
    refundable: bool = Field(False, description="Whether ticket is cash/card refundable")
    credit_only: bool = Field(False, description="Whether refund is issued as future flight credit only")
    cancellation_cutoff_minutes: int = Field(0, ge=0, description="Minimum minutes before departure for cancellation")


class FareRuleUpdate(BaseModel):
    price: Optional[Decimal] = Field(None, ge=0, decimal_places=2)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    changes_allowed: Optional[bool] = None
    seat_selection_allowed: Optional[bool] = None
    refundable: Optional[bool] = None
    credit_only: Optional[bool] = None
    cancellation_cutoff_minutes: Optional[int] = Field(None, ge=0)


class FareRuleResponse(BaseModel):
    id: UUID
    flight_id: UUID
    class_type: FlightClassType
    fare_type: FareType
    price: Decimal
    currency: str
    changes_allowed: bool
    seat_selection_allowed: bool
    refundable: bool
    credit_only: bool
    cancellation_cutoff_minutes: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
