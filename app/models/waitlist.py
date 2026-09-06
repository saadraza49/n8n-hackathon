import uuid
from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import FlightClassType, WaitlistEntryType, WaitlistStatus


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    __table_args__ = (
        Index(
            "ix_waitlist_queue",
            "flight_id",
            "class_type",
            "status",
            "priority_score",
            "joined_at",
            "id",
        ),
        Index("ix_waitlist_passenger_status", "passenger_id", "status"),
        Index("ix_waitlist_claim_deadline", "status", "claim_deadline"),
        Index(
            "uq_waitlist_active_passenger",
            "passenger_id",
            "flight_id",
            "class_type",
            unique=True,
            sqlite_where=text("status IN ('WAITING', 'PROMOTED', 'CLAIMED')"),
            postgresql_where=text("status IN ('WAITING', 'PROMOTED', 'CLAIMED')"),
        ),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="CASCADE"), nullable=False, index=True)
    passenger_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    class_type = Column(
        SAEnum(FlightClassType, name="flight_class_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    entry_type = Column(
        SAEnum(WaitlistEntryType, name="waitlist_entry_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=WaitlistEntryType.WAITLIST,
        server_default=WaitlistEntryType.WAITLIST.value,
        index=True,
    )
    status = Column(
        SAEnum(WaitlistStatus, name="waitlist_status", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=WaitlistStatus.WAITING,
        server_default=WaitlistStatus.WAITING.value,
        index=True,
    )
    priority_score = Column(Integer, nullable=False, default=0, server_default="0", index=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    promoted_at = Column(DateTime(timezone=True), nullable=True)
    claim_deadline = Column(DateTime(timezone=True), nullable=True, index=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    promoted_seat_id = Column(Uuid(as_uuid=True), ForeignKey("flight_seats.id", ondelete="SET NULL"), nullable=True, index=True)
    booking_id = Column(Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, index=True)
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
    passenger = relationship("User", back_populates="waitlist_entries")
    flight = relationship("Flight", back_populates="waitlist_entries")
    promoted_seat = relationship("FlightSeat", foreign_keys=[promoted_seat_id])
    booking = relationship("Booking", foreign_keys=[booking_id])
