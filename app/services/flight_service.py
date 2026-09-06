from datetime import datetime, timedelta, timezone
from math import ceil
from typing import List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingItem
from app.models.enums import BookingStatus, FlightClassType, FlightStatus, SeatStatus
from app.models.flight import Flight

from app.models.flight_class import FlightClass
from app.models.flight_seat import FlightSeat
from app.schemas.flight import FlightCreate, FlightUpdate
from app.services.seat_generator import generate_physical_seats


class FlightService:
    @staticmethod
    def create_flight(db: Session, payload: FlightCreate, actor_id: UUID) -> Flight:
        """Create a new flight with class inventories, deterministic physical seats, and audit log atomically."""
        # 1. Duplicate flight detection (same flight number, route, and UTC calendar date)
        dep_utc = payload.departure_at.astimezone(timezone.utc)
        day_start = datetime(dep_utc.year, dep_utc.month, dep_utc.day, 0, 0, 0, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)

        existing = (
            db.query(Flight)
            .filter(
                Flight.flight_number == payload.flight_number,
                Flight.origin == payload.origin,
                Flight.destination == payload.destination,
                Flight.departure_at >= day_start,
                Flight.departure_at < day_end,
                Flight.status != FlightStatus.CANCELLED,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Duplicate flight: Flight {payload.flight_number} from {payload.origin} to {payload.destination} "
                    f"is already scheduled on {day_start.strftime('%Y-%m-%d')}."
                ),
            )

        # 2. Atomic creation of flight, class inventories, seats, and audit log
        try:
            # Flight record
            flight = Flight(
                flight_number=payload.flight_number,
                origin=payload.origin,
                destination=payload.destination,
                departure_at=payload.departure_at,
                arrival_at=payload.arrival_at,
                total_capacity=payload.total_capacity,
                status=FlightStatus.SCHEDULED,
                created_by=actor_id,
            )
            db.add(flight)
            db.flush()  # Populates flight.id for foreign keys

            # Class inventory records (available_seats = total_seats)
            classes = [
                FlightClass(
                    flight_id=flight.id,
                    class_type=FlightClassType.FIRST,
                    total_seats=payload.first_class_seats,
                    available_seats=payload.first_class_seats,
                ),
                FlightClass(
                    flight_id=flight.id,
                    class_type=FlightClassType.BUSINESS,
                    total_seats=payload.business_class_seats,
                    available_seats=payload.business_class_seats,
                ),
                FlightClass(
                    flight_id=flight.id,
                    class_type=FlightClassType.ECONOMY,
                    total_seats=payload.economy_class_seats,
                    available_seats=payload.economy_class_seats,
                ),
            ]
            db.add_all(classes)

            # Deterministic physical seat generation
            seats = generate_physical_seats(
                flight_id=flight.id,
                first_seats_count=payload.first_class_seats,
                business_seats_count=payload.business_class_seats,
                economy_seats_count=payload.economy_class_seats,
            )
            db.add_all(seats)

            # Audit log record
            audit_log = AuditLog(
                user_id=actor_id,
                action="CREATE_FLIGHT",
                entity_type="FLIGHT",
                entity_id=flight.id,
                old_values=None,
                new_values={
                    "flight_number": flight.flight_number,
                    "origin": flight.origin,
                    "destination": flight.destination,
                    "departure_at": flight.departure_at.isoformat(),
                    "arrival_at": flight.arrival_at.isoformat(),
                    "total_capacity": flight.total_capacity,
                    "first_class_seats": payload.first_class_seats,
                    "business_class_seats": payload.business_class_seats,
                    "economy_class_seats": payload.economy_class_seats,
                    "status": flight.status.value,
                },
            )
            db.add(audit_log)

            db.commit()
            db.refresh(flight)
            return flight

        except Exception:
            db.rollback()
            raise

    @staticmethod
    def get_flight_by_id(db: Session, flight_id: UUID) -> Flight:
        """Retrieve a flight with its class breakdown by ID."""
        flight = (
            db.query(Flight)
            .options(joinedload(Flight.classes))
            .filter(Flight.id == flight_id)
            .first()
        )
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flight with id '{flight_id}' not found.",
            )
        return flight

    @staticmethod
    def get_flight_details(db: Session, flight_id: UUID) -> Tuple[Flight, int, int]:
        """Retrieve a flight along with total and available physical seat counts."""
        flight = FlightService.get_flight_by_id(db, flight_id)
        total_seats = (
            db.query(func.count(FlightSeat.id))
            .filter(FlightSeat.flight_id == flight_id)
            .scalar()
            or 0
        )
        available_seats = (
            db.query(func.count(FlightSeat.id))
            .filter(
                FlightSeat.flight_id == flight_id,
                FlightSeat.status == SeatStatus.AVAILABLE,
            )
            .scalar()
            or 0
        )
        return flight, total_seats, available_seats

    @staticmethod
    def list_flights(
        db: Session,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        flight_number: Optional[str] = None,
        status_filter: Optional[FlightStatus] = None,
        departure_from: Optional[datetime] = None,
        departure_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[list[Flight], int, int]:
        """List and search flights with filtering and pagination."""
        query = db.query(Flight).options(joinedload(Flight.classes))

        if origin:
            query = query.filter(Flight.origin == origin.strip().upper())
        if destination:
            query = query.filter(Flight.destination == destination.strip().upper())
        if flight_number:
            query = query.filter(Flight.flight_number.ilike(f"%{flight_number.strip()}%"))
        if status_filter:
            query = query.filter(Flight.status == status_filter)
        if departure_from:
            query = query.filter(Flight.departure_at >= departure_from)
        if departure_to:
            query = query.filter(Flight.departure_at <= departure_to)

        total = query.count()
        total_pages = ceil(total / page_size) if total > 0 else 1
        offset = (page - 1) * page_size

        flights = query.order_by(Flight.departure_at.asc()).offset(offset).limit(page_size).all()
        return flights, total, total_pages

    @staticmethod
    def update_flight(
        db: Session, flight_id: UUID, payload: FlightUpdate, actor_id: UUID
    ) -> Flight:
        """Update flight details and record administrative audit log."""
        flight = (
            db.query(Flight)
            .options(joinedload(Flight.classes))
            .filter(Flight.id == flight_id)
            .first()
        )
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flight with id '{flight_id}' not found.",
            )

        # Terminal status guards
        if flight.status == FlightStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot update a CANCELLED flight.",
            )
        if flight.status == FlightStatus.COMPLETED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot update a COMPLETED flight.",
            )

        # Datetime validations against existing values
        new_dep = payload.departure_at or flight.departure_at
        new_arr = payload.arrival_at or flight.arrival_at
        if new_arr <= new_dep:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="arrival_at must be strictly later than departure_at.",
            )

        old_values = {
            "flight_number": flight.flight_number,
            "origin": flight.origin,
            "destination": flight.destination,
            "departure_at": flight.departure_at.isoformat(),
            "arrival_at": flight.arrival_at.isoformat(),
            "status": flight.status.value,
        }

        new_values = {}
        if payload.flight_number is not None:
            flight.flight_number = payload.flight_number
            new_values["flight_number"] = flight.flight_number
        if payload.origin is not None:
            flight.origin = payload.origin
            new_values["origin"] = flight.origin
        if payload.destination is not None:
            flight.destination = payload.destination
            new_values["destination"] = flight.destination
        if payload.departure_at is not None:
            flight.departure_at = payload.departure_at
            new_values["departure_at"] = flight.departure_at.isoformat()
        if payload.arrival_at is not None:
            flight.arrival_at = payload.arrival_at
            new_values["arrival_at"] = flight.arrival_at.isoformat()
        if payload.status is not None:
            flight.status = payload.status
            new_values["status"] = flight.status.value

        if new_values:
            audit = AuditLog(
                user_id=actor_id,
                action="UPDATE_FLIGHT",
                entity_type="FLIGHT",
                entity_id=flight.id,
                old_values=old_values,
                new_values=new_values,
            )
            db.add(audit)

        db.commit()
        db.refresh(flight)
        return flight

    @staticmethod
    def cancel_flight(db: Session, flight_id: UUID, actor_id: UUID) -> Flight:
        """Cancel a flight without deleting historical records, recording an audit log."""
        flight = (
            db.query(Flight)
            .options(joinedload(Flight.classes))
            .filter(Flight.id == flight_id)
            .first()
        )
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Flight with id '{flight_id}' not found.",
            )

        if flight.status == FlightStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Flight is already cancelled.",
            )

        if flight.status == FlightStatus.COMPLETED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot cancel a completed flight.",
            )

        old_status = flight.status.value
        flight.status = FlightStatus.CANCELLED

        # Identify confirmed bookings impacted by flight cancellation
        affected_count = (
            db.query(func.count(Booking.id))
            .filter(Booking.flight_id == flight_id, Booking.status == BookingStatus.CONFIRMED)
            .scalar()
            or 0
        )

        audit = AuditLog(
            user_id=actor_id,
            action="CANCEL_FLIGHT",
            entity_type="FLIGHT",
            entity_id=flight.id,
            old_values={"status": old_status},
            new_values={
                "status": FlightStatus.CANCELLED.value,
                "affected_bookings_count": affected_count,
            },
        )
        db.add(audit)

        db.commit()
        db.refresh(flight)
        return flight

    @staticmethod
    def get_affected_bookings(db: Session, flight_id: UUID) -> List[Booking]:
        """Retrieve all confirmed bookings impacted by a flight schedule change or cancellation."""
        flight = FlightService.get_flight_by_id(db, flight_id)
        bookings = (
            db.query(Booking)
            .filter(
                Booking.flight_id == flight_id,
                Booking.status == BookingStatus.CONFIRMED,
            )
            .options(selectinload(Booking.items).selectinload(BookingItem.seat))
            .all()
        )
        return bookings

