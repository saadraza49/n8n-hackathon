import uuid
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import FlightStatus


class Flight(Base):
    __tablename__ = "flights"

    __table_args__ = (
        CheckConstraint("total_capacity > 0", name="ck_flights_total_capacity_positive"),
        CheckConstraint("arrival_at > departure_at", name="ck_flights_arrival_after_departure"),
        CheckConstraint("origin != destination", name="ck_flights_origin_ne_destination"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_number = Column(String(20), nullable=False, index=True)
    origin = Column(String(10), nullable=False, index=True)
    destination = Column(String(10), nullable=False, index=True)
    origin_timezone = Column(String(50), nullable=False, default="UTC", server_default="UTC")
    destination_timezone = Column(String(50), nullable=False, default="UTC", server_default="UTC")
    departure_at = Column(DateTime(timezone=True), nullable=False, index=True)
    arrival_at = Column(DateTime(timezone=True), nullable=False)
    total_capacity = Column(Integer, nullable=False)
    status = Column(
        SAEnum(FlightStatus, name="flight_status", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=FlightStatus.SCHEDULED,
        server_default=FlightStatus.SCHEDULED.value,
        index=True,
    )
    created_by = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    creator = relationship("User", back_populates="created_flights")
    classes = relationship("FlightClass", back_populates="flight", cascade="all, delete-orphan")
    seats = relationship("FlightSeat", back_populates="flight", cascade="all, delete-orphan")
    fare_rules = relationship("FareRule", back_populates="flight", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="flight")
    waitlist_entries = relationship("WaitlistEntry", back_populates="flight", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="flight", cascade="all, delete-orphan")
    price_histories = relationship("PriceHistory", back_populates="flight", cascade="all, delete-orphan")


