from datetime import datetime
from typing import List
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import FlightClassType, FlightStatus
from app.schemas.fare_rule import FareRuleResponse


class FlightSearchResultFlight(BaseModel):
    id: UUID
    flight_number: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    status: FlightStatus

    model_config = ConfigDict(from_attributes=True)


class FlightClassSearchResult(BaseModel):
    class_type: FlightClassType
    total_seats: int
    available_seats: int
    fares: List[FareRuleResponse] = []

    model_config = ConfigDict(from_attributes=True)


class FlightSearchResultItem(BaseModel):
    flight: FlightSearchResultFlight
    classes: List[FlightClassSearchResult] = []


class SearchPagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class FlightSearchResponse(BaseModel):
    items: List[FlightSearchResultItem]
    pagination: SearchPagination
