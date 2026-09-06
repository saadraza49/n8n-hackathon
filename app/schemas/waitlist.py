from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FareType, FlightClassType, WaitlistEntryType, WaitlistStatus


class WaitlistJoinRequest(BaseModel):
    class_type: FlightClassType
    entry_type: WaitlistEntryType = WaitlistEntryType.WAITLIST
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None


class WaitlistEntryResponse(BaseModel):
    id: UUID
    flight_id: UUID
    passenger_id: UUID
    class_type: FlightClassType
    entry_type: WaitlistEntryType
    status: WaitlistStatus
    priority_score: int
    queue_position: Optional[int] = None
    joined_at: datetime
    promoted_at: Optional[datetime] = None
    claim_deadline: Optional[datetime] = None
    claimed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    promoted_seat_id: Optional[UUID] = None
    booking_id: Optional[UUID] = None
    notes: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class WaitlistPromoteRequest(BaseModel):
    class_type: FlightClassType
    claim_window_minutes: Optional[int] = Field(default=15, ge=1, le=1440)


class WaitlistClaimRequest(BaseModel):
    fare_type: FareType = FareType.FLEXIBLE
    passenger_name: Optional[str] = None
    idempotency_key: Optional[str] = None


class WaitlistClaimResponse(BaseModel):
    waitlist_id: UUID
    booking_id: UUID
    booking_reference: str
    seat_id: UUID
    seat_number: str
    class_type: FlightClassType
    fare_type: FareType
    total_amount: Decimal
    currency: str
    message: str

    model_config = ConfigDict(from_attributes=True)


class WaitlistFlightQueueResponse(BaseModel):
    flight_id: UUID
    flight_number: str
    class_type: Optional[FlightClassType] = None
    total_waiting: int
    total_promoted: int
    entries: List[WaitlistEntryResponse]
