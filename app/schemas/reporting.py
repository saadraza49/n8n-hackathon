from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FlightOperationalMetricsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    flight_id: UUID
    flight_number: str
    origin: str
    destination: str
    origin_timezone: str
    destination_timezone: str
    departure_at: datetime
    arrival_at: datetime
    flight_status: str
    total_capacity: int
    booked_seats: int
    held_seats: int
    available_seats: int
    load_factor_percentage: Decimal = Field(..., max_digits=5, decimal_places=2)
    gross_revenue: Decimal = Field(..., max_digits=12, decimal_places=2)
    total_refunded: Decimal = Field(..., max_digits=12, decimal_places=2)
    net_revenue: Decimal = Field(..., max_digits=12, decimal_places=2)


class EligibleCheckinReminderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    booking_id: UUID
    booking_reference: str
    passenger_id: UUID
    passenger_name: str
    passenger_email: str
    flight_id: UUID
    flight_number: str
    origin: str
    destination: str
    origin_timezone: str
    destination_timezone: str
    departure_at: datetime
    arrival_at: datetime
    flight_status: str
    deduplication_key: str


class DailyOperationalSummaryResponse(BaseModel):
    total_flights: int
    total_booked_passengers: int
    average_load_factor: Decimal = Field(..., max_digits=5, decimal_places=2)
    total_gross_revenue: Decimal = Field(..., max_digits=14, decimal_places=2)
    total_refunds: Decimal = Field(..., max_digits=14, decimal_places=2)
    total_net_revenue: Decimal = Field(..., max_digits=14, decimal_places=2)
    flights: List[FlightOperationalMetricsResponse] = []
