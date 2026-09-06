import uuid
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FareType,
    FlightClassType,
    RefundReason,
    RefundStatus,
    RefundType,
)


class Booking(Base):
    __tablename__ = "bookings"

    __table_args__ = (
        UniqueConstraint("booking_reference", name="uq_bookings_booking_reference"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_bookings_user_idempotency"),
        CheckConstraint("total_amount >= 0", name="ck_bookings_total_amount_non_negative"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_reference = Column(String(10), nullable=False, index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="RESTRICT"), nullable=False, index=True)
    status = Column(
        SAEnum(BookingStatus, name="booking_status", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=BookingStatus.PENDING,
        server_default=BookingStatus.PENDING.value,
        index=True,
    )
    total_amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    idempotency_key = Column(String(255), nullable=True, index=True)
    hold_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user = relationship("User", back_populates="bookings")
    flight = relationship("Flight", back_populates="bookings")
    items = relationship("BookingItem", back_populates="booking", cascade="all, delete-orphan")
    refunds = relationship("Refund", back_populates="booking", cascade="all, delete-orphan")
    changes = relationship("BookingChange", back_populates="booking", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="booking", cascade="all, delete-orphan")
    fraud_evaluations = relationship(
        "FraudEvaluation",
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="desc(FraudEvaluation.evaluated_at)",
    )


class BookingItem(Base):
    __tablename__ = "booking_items"

    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_booking_items_price_non_negative"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="RESTRICT"), nullable=False, index=True)
    seat_id = Column(Uuid(as_uuid=True), ForeignKey("flight_seats.id", ondelete="RESTRICT"), nullable=False, index=True)
    class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    fare_type = Column(
        SAEnum(FareType, name="fare_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    status = Column(
        SAEnum(BookingItemStatus, name="booking_item_status", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=BookingItemStatus.CONFIRMED,
        server_default=BookingItemStatus.CONFIRMED.value,
        index=True,
    )
    passenger_name = Column(String(255), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    booking = relationship("Booking", back_populates="items")
    flight = relationship("Flight")
    seat = relationship("FlightSeat")
    refunds = relationship("Refund", back_populates="booking_item")
    changes = relationship("BookingChange", back_populates="booking_item")


class Refund(Base):
    __tablename__ = "refunds"

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_refunds_amount_non_negative"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False, index=True)
    booking_item_id = Column(Uuid(as_uuid=True), ForeignKey("booking_items.id", ondelete="RESTRICT"), nullable=True, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    refund_type = Column(
        SAEnum(RefundType, name="refund_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    status = Column(
        SAEnum(RefundStatus, name="refund_status", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=RefundStatus.PENDING,
        server_default=RefundStatus.PENDING.value,
        index=True,
    )
    reason = Column(
        SAEnum(RefundReason, name="refund_reason", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=RefundReason.CUSTOMER_CANCELLATION,
        server_default=RefundReason.CUSTOMER_CANCELLATION.value,
        index=True,
    )
    idempotency_key = Column(String(255), nullable=True, index=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    booking = relationship("Booking", back_populates="refunds")
    booking_item = relationship("BookingItem", back_populates="refunds")


class BookingChange(Base):
    __tablename__ = "booking_changes"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False, index=True)
    booking_item_id = Column(Uuid(as_uuid=True), ForeignKey("booking_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    old_flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="RESTRICT"), nullable=False)
    new_flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="RESTRICT"), nullable=False)
    old_seat_id = Column(Uuid(as_uuid=True), ForeignKey("flight_seats.id", ondelete="RESTRICT"), nullable=False)
    new_seat_id = Column(Uuid(as_uuid=True), ForeignKey("flight_seats.id", ondelete="RESTRICT"), nullable=False)
    old_class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    new_class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    old_fare_type = Column(
        SAEnum(FareType, name="fare_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    new_fare_type = Column(
        SAEnum(FareType, name="fare_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    old_price = Column(Numeric(10, 2), nullable=False)
    new_price = Column(Numeric(10, 2), nullable=False)
    price_difference = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    idempotency_key = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    booking = relationship("Booking", back_populates="changes")
    booking_item = relationship("BookingItem", back_populates="changes")
    old_flight = relationship("Flight", foreign_keys=[old_flight_id])
    new_flight = relationship("Flight", foreign_keys=[new_flight_id])
    old_seat = relationship("FlightSeat", foreign_keys=[old_seat_id])
    new_seat = relationship("FlightSeat", foreign_keys=[new_seat_id])
