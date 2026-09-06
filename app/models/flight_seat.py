import uuid
from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import FlightClassType, SeatStatus


class FlightSeat(Base):
    __tablename__ = "flight_seats"

    __table_args__ = (
        UniqueConstraint("flight_id", "seat_number", name="uq_flight_seats_flight_seat"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="CASCADE"), nullable=False, index=True)
    seat_number = Column(String(10), nullable=False)
    class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    status = Column(
        SAEnum(SeatStatus, name="seat_status", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=SeatStatus.AVAILABLE,
        server_default=SeatStatus.AVAILABLE.value,
        index=True,
    )
    hold_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    flight = relationship("Flight", back_populates="seats")
