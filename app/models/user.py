import uuid
from sqlalchemy import Column, DateTime, Enum as SAEnum, String, Uuid, func
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import UserRole


class User(Base):
    __tablename__ = "users"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(
        SAEnum(UserRole, name="user_role", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=UserRole.PASSENGER,
        server_default=UserRole.PASSENGER.value,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    created_flights = relationship("Flight", back_populates="creator")
    audit_logs = relationship("AuditLog", back_populates="user")
    bookings = relationship("Booking", back_populates="user")
    waitlist_entries = relationship("WaitlistEntry", back_populates="passenger")



