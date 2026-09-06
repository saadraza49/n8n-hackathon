import uuid
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import FareType, FlightClassType, PriceChangeReason


class PriceHistory(Base):
    __tablename__ = "price_history"

    __table_args__ = (
        CheckConstraint("old_price >= 0", name="ck_price_history_old_price_non_negative"),
        CheckConstraint("new_price >= 0", name="ck_price_history_new_price_non_negative"),
        Index("ix_price_history_flight_class_time", "flight_id", "class_type", "created_at"),
        Index("ix_price_history_fare_rule", "fare_rule_id", "created_at"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fare_rule_id = Column(Uuid(as_uuid=True), ForeignKey("fare_rules.id", ondelete="CASCADE"), nullable=False, index=True)
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="CASCADE"), nullable=False, index=True)
    class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    fare_type = Column(
        SAEnum(FareType, name="fare_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    old_price = Column(Numeric(10, 2), nullable=False)
    new_price = Column(Numeric(10, 2), nullable=False)
    price_delta = Column(Numeric(10, 2), nullable=False)
    percentage_drop = Column(Numeric(5, 2), nullable=True)
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    changed_by = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    reason = Column(
        SAEnum(PriceChangeReason, name="price_change_reason", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=PriceChangeReason.MANUAL_UPDATE,
        server_default=PriceChangeReason.MANUAL_UPDATE.value,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Relationships
    fare_rule = relationship("FareRule", back_populates="price_histories")
    flight = relationship("Flight", back_populates="price_histories")
    modifier = relationship("User", foreign_keys=[changed_by])
