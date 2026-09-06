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
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import FraudDecision, RiskLevel


class FraudEvaluation(Base):
    __tablename__ = "fraud_evaluations"

    __table_args__ = (
        CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_fraud_evaluations_score_range"),
        Index("ix_fraud_evaluations_booking_evaluated", "booking_id", "evaluated_at"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(Uuid(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    risk_score = Column(Integer, nullable=False)
    risk_level = Column(
        SAEnum(RiskLevel, name="risk_level", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    decision = Column(
        SAEnum(FraudDecision, name="fraud_decision", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    reasons = Column(JSON, nullable=False)
    evaluator = Column(String(100), nullable=False, default="RULE_ENGINE_V1", server_default="RULE_ENGINE_V1")
    idempotency_key = Column(String(255), nullable=True, index=True)
    evaluated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    booking = relationship("Booking", back_populates="fraud_evaluations")
    user = relationship("User", foreign_keys=[user_id])
    signals = relationship("RiskSignalRecord", back_populates="evaluation", cascade="all, delete-orphan")


class RiskSignalRecord(Base):
    __tablename__ = "risk_signals"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluation_id = Column(Uuid(as_uuid=True), ForeignKey("fraud_evaluations.id", ondelete="CASCADE"), nullable=False, index=True)
    signal_code = Column(String(100), nullable=False, index=True)
    severity = Column(String(50), nullable=False)
    score_contribution = Column(Integer, nullable=False)
    description = Column(String(500), nullable=False)
    evidence = Column(JSON, nullable=True)
    signal_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    evaluation = relationship("FraudEvaluation", back_populates="signals")

