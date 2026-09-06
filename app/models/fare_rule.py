import uuid
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import FareType, FlightClassType


class FareRule(Base):
    __tablename__ = "fare_rules"

    __table_args__ = (
        UniqueConstraint("flight_id", "class_type", "fare_type", name="uq_fare_rules_flight_class_fare"),
        CheckConstraint("price >= 0", name="ck_fare_rules_price_non_negative"),
        CheckConstraint("cancellation_cutoff_minutes >= 0", name="ck_fare_rules_cancellation_cutoff_non_negative"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="CASCADE"), nullable=False, index=True)
    class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    fare_type = Column(
        SAEnum(FareType, name="fare_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    price = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    changes_allowed = Column(Boolean, nullable=False, default=False)
    seat_selection_allowed = Column(Boolean, nullable=False, default=False)
    refundable = Column(Boolean, nullable=False, default=False)
    credit_only = Column(Boolean, nullable=False, default=False)
    cancellation_cutoff_minutes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    flight = relationship("Flight", back_populates="fare_rules")
    price_histories = relationship("PriceHistory", back_populates="fare_rule", cascade="all, delete-orphan")
