import uuid
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import FlightClassType


class FlightClass(Base):
    __tablename__ = "flight_classes"

    __table_args__ = (
        UniqueConstraint("flight_id", "class_type", name="uq_flight_classes_flight_class"),
        CheckConstraint("total_seats > 0", name="ck_flight_classes_total_seats_positive"),
        CheckConstraint("available_seats >= 0", name="ck_flight_classes_available_seats_non_negative"),
        CheckConstraint("available_seats <= total_seats", name="ck_flight_classes_available_lte_total"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="CASCADE"), nullable=False, index=True)
    class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    total_seats = Column(Integer, nullable=False)
    available_seats = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    flight = relationship("Flight", back_populates="classes")
