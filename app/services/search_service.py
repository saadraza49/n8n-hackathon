from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.enums import FlightStatus
from app.models.flight import Flight
from app.schemas.fare_rule import FareRuleResponse
from app.schemas.search import (
    FlightClassSearchResult,
    FlightSearchResponse,
    FlightSearchResultFlight,
    FlightSearchResultItem,
    SearchPagination,
)


class FlightSearchService:
    @staticmethod
    def search_flights(
        db: Session,
        origin: str,
        destination: str,
        departure_date: date,
        departure_from: Optional[datetime] = None,
        departure_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> FlightSearchResponse:
        """Search passenger flights with timezone-safe date filtering, availability, and fare options.
        
        Guarantees:
          - Strictly READ-ONLY: Never modifies seats, availability, or holds.
          - Zero N+1 queries: Uses selectinload for batch fetching of classes and fare rules.
          - Excludes CANCELLED and COMPLETED flights.
        """
        norm_origin = origin.strip().upper()
        norm_destination = destination.strip().upper()

        if not norm_origin or not norm_destination:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Origin and destination must be non-empty airport codes.",
            )

        if norm_origin == norm_destination:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Origin and destination cannot be identical ({norm_origin}).",
            )

        if departure_from and departure_to and departure_to < departure_from:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="departure_to must be later than or equal to departure_from.",
            )

        # Timezone-safe UTC date window: [day_start, day_end)
        day_start = datetime.combine(departure_date, time.min).replace(tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)

        # Base filter condition
        filters = [
            Flight.origin == norm_origin,
            Flight.destination == norm_destination,
            Flight.departure_at >= day_start,
            Flight.departure_at < day_end,
            Flight.status.not_in([FlightStatus.CANCELLED, FlightStatus.COMPLETED]),
        ]

        if departure_from:
            filters.append(Flight.departure_at >= departure_from)
        if departure_to:
            filters.append(Flight.departure_at <= departure_to)

        # 1. Total count query
        total = db.query(func.count(Flight.id)).filter(*filters).scalar() or 0
        total_pages = ceil(total / page_size) if total > 0 else 0

        # If no flights found, return empty results immediately
        if total == 0:
            return FlightSearchResponse(
                items=[],
                pagination=SearchPagination(
                    page=page,
                    page_size=page_size,
                    total=0,
                    total_pages=0,
                ),
            )

        # 2. Efficient batch query preventing N+1 queries
        offset = (page - 1) * page_size
        flights = (
            db.query(Flight)
            .options(
                selectinload(Flight.classes),
                selectinload(Flight.fare_rules),
            )
            .filter(*filters)
            .order_by(Flight.departure_at.asc())
            .offset(offset)
            .limit(page_size)
            .all()
        )

        # 3. Assemble structured frontend-friendly response
        items: list[FlightSearchResultItem] = []
        for flight in flights:
            classes_result: list[FlightClassSearchResult] = []
            for cls in flight.classes:
                # Associate configured fare rules for this specific class
                matching_fares = [
                    FareRuleResponse.model_validate(fr)
                    for fr in flight.fare_rules
                    if fr.class_type == cls.class_type
                ]
                classes_result.append(
                    FlightClassSearchResult(
                        class_type=cls.class_type,
                        total_seats=cls.total_seats,
                        available_seats=cls.available_seats,
                        fares=matching_fares,
                    )
                )

            items.append(
                FlightSearchResultItem(
                    flight=FlightSearchResultFlight.model_validate(flight),
                    classes=classes_result,
                )
            )

        return FlightSearchResponse(
            items=items,
            pagination=SearchPagination(
                page=page,
                page_size=page_size,
                total=total,
                total_pages=total_pages,
            ),
        )
