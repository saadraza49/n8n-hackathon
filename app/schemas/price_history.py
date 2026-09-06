from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FareType, FlightClassType, PriceChangeReason


class PriceHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fare_rule_id: UUID
    flight_id: UUID
    class_type: FlightClassType
    fare_type: FareType
    old_price: Decimal = Field(..., max_digits=10, decimal_places=2)
    new_price: Decimal = Field(..., max_digits=10, decimal_places=2)
    price_delta: Decimal = Field(..., max_digits=10, decimal_places=2)
    percentage_drop: Optional[Decimal] = Field(None, max_digits=5, decimal_places=2)
    currency: str
    changed_by: Optional[UUID] = None
    reason: PriceChangeReason
    created_at: datetime
