import uuid
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import NotificationChannel, NotificationStatus, NotificationType


class Notification(Base):
    __tablename__ = "notifications"

    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uq_notifications_deduplication_key"),
        CheckConstraint("attempt_count >= 0", name="ck_notifications_attempt_count_non_negative"),
        CheckConstraint("max_retries >= 0", name="ck_notifications_max_retries_non_negative"),
        Index("ix_notifications_claim_worker", "status", "scheduled_for", "attempt_count"),
        Index("ix_notifications_flight_type", "flight_id", "notification_type"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    notification_type = Column(
        SAEnum(NotificationType, name="notification_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    channel = Column(
        SAEnum(NotificationChannel, name="notification_channel", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=NotificationChannel.EMAIL,
        server_default=NotificationChannel.EMAIL.value,
    )
    recipient = Column(String(255), nullable=False, index=True)
    status = Column(
        SAEnum(NotificationStatus, name="notification_status", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
        index=True,
    )
    flight_id = Column(Uuid(as_uuid=True), ForeignKey("flights.id", ondelete="SET NULL"), nullable=True, index=True)
    booking_id = Column(Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, index=True)
    deduplication_key = Column(String(255), nullable=False, unique=True, index=True)
    payload = Column(JSON, nullable=True)

    scheduled_for = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_retries = Column(Integer, nullable=False, default=3, server_default="3")
    last_error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user = relationship("User", foreign_keys=[user_id])
    flight = relationship("Flight", back_populates="notifications")
    booking = relationship("Booking", back_populates="notifications")
