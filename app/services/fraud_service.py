import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session, selectinload

from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingChange, Refund
from app.models.enums import FraudDecision, RefundStatus, RiskLevel
from app.models.fraud import FraudEvaluation
from app.models.user import User
from app.schemas.fraud import (
    FraudEvaluationListResponse,
    FraudEvaluationResponse,
    FraudReasonDetails,
    FraudSignal,
    PaginatedFraudEvaluationResponse,
)

EVALUATOR_VERSION = "RULE_ENGINE_V1"


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Helper to ensure datetime is timezone-aware in UTC for safe comparisons across dialects."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_evaluation_response(record: FraudEvaluation, booking_reference: Optional[str] = None) -> FraudEvaluationResponse:
    """Format SQLAlchemy FraudEvaluation into typed Pydantic response."""
    reasons_data = record.reasons
    if isinstance(reasons_data, dict):
        signals_raw = reasons_data.get("signals", [])
        signals = [FraudSignal(**s) for s in signals_raw]
        summary = reasons_data.get("summary", "")
        reasons_obj = FraudReasonDetails(signals=signals, summary=summary)
    else:
        reasons_obj = FraudReasonDetails(signals=[], summary=str(reasons_data))

    ref = booking_reference
    if not ref and record.booking:
        ref = record.booking.booking_reference

    return FraudEvaluationResponse(
        id=record.id,
        booking_id=record.booking_id,
        booking_reference=ref,
        user_id=record.user_id,
        risk_score=record.risk_score,
        risk_level=record.risk_level,
        decision=record.decision,
        reasons=reasons_obj,
        evaluator=record.evaluator,
        idempotency_key=record.idempotency_key,
        evaluated_at=record.evaluated_at,
        created_at=record.created_at,
    )


def evaluate_booking_risk(
    db: Session,
    booking_id: UUID,
    current_user: Optional[User] = None,
    source: Optional[str] = None,
    force_re_evaluate: bool = False,
    idempotency_key: Optional[str] = None,
) -> FraudEvaluationResponse:
    """
    Evaluate fraud and risk for a specific booking using deterministic, explainable signals
    derived strictly from the existing database.

    Idempotency:
    - If `idempotency_key` matches an existing evaluation for this booking, returns it directly.
    - If `idempotency_key` was reused with a different booking, raises 409 Conflict.
    - If `force_re_evaluate` is False and an evaluation already exists, returns the latest evaluation.

    Concurrency:
    - Serializes evaluation for the same booking using row-level locking.
    """
    # 1. Check Idempotency Key Replay first
    if idempotency_key:
        existing_by_key = (
            db.query(FraudEvaluation)
            .filter(FraudEvaluation.idempotency_key == idempotency_key)
            .first()
        )
        if existing_by_key:
            if existing_by_key.booking_id != booking_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key reused with different booking",
                )
            return _format_evaluation_response(existing_by_key)

    # 2. Acquire lock on Booking for concurrency protection across simultaneous calls
    query = db.query(Booking).filter(Booking.id == booking_id)
    if db.bind and db.bind.dialect.name != "sqlite":
        # PostgreSQL supports row-level locks
        query = query.with_for_update(read=True)

    booking = (
        query.options(
            selectinload(Booking.items),
            selectinload(Booking.flight),
            selectinload(Booking.user),
            selectinload(Booking.changes),
        )
        .first()
    )

    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking with ID '{booking_id}' not found",
        )

    # 3. Return latest evaluation if not forcing re-evaluation
    if not force_re_evaluate and not idempotency_key:
        existing = (
            db.query(FraudEvaluation)
            .filter(FraudEvaluation.booking_id == booking_id)
            .order_by(desc(FraudEvaluation.evaluated_at))
            .first()
        )
        if existing:
            return _format_evaluation_response(existing, booking.booking_reference)

    signals: List[FraudSignal] = []

    # Signal 1: Last-minute departure
    if booking.flight and booking.flight.departure_at and booking.created_at:
        flight_dep = _ensure_utc(booking.flight.departure_at)
        booking_created = _ensure_utc(booking.created_at)
        time_to_dep = flight_dep - booking_created
        hours_to_dep = round(time_to_dep.total_seconds() / 3600.0, 1)

        if time_to_dep < timedelta(hours=6):
            signals.append(
                FraudSignal(
                    code="LAST_MINUTE_DEPARTURE",
                    weight=25,
                    observed_value=f"{hours_to_dep} hours",
                    threshold="< 6.0 hours",
                    description=f"Booking created within 6 hours of departure ({hours_to_dep}h prior)",
                    metadata={"hours_prior": hours_to_dep},
                )
            )
        elif time_to_dep < timedelta(hours=24):
            signals.append(
                FraudSignal(
                    code="LAST_MINUTE_DEPARTURE",
                    weight=15,
                    observed_value=f"{hours_to_dep} hours",
                    threshold="< 24.0 hours",
                    description=f"Booking created within 24 hours of departure ({hours_to_dep}h prior)",
                    metadata={"hours_prior": hours_to_dep},
                )
            )

    # Signal 2: High transaction value
    total_val = Decimal(str(booking.total_amount))
    if total_val >= Decimal("5000.00"):
        signals.append(
            FraudSignal(
                code="HIGH_TRANSACTION_VALUE",
                weight=35,
                observed_value=f"{total_val} {booking.currency}",
                threshold=">= 5000.00",
                description=f"Very high transaction exposure of {total_val} {booking.currency} (>= 5000)",
                metadata={"total_amount": float(total_val), "currency": booking.currency},
            )
        )
    elif total_val >= Decimal("3000.00"):
        signals.append(
            FraudSignal(
                code="HIGH_TRANSACTION_VALUE",
                weight=25,
                observed_value=f"{total_val} {booking.currency}",
                threshold=">= 3000.00",
                description=f"High transaction exposure of {total_val} {booking.currency} (>= 3000)",
                metadata={"total_amount": float(total_val), "currency": booking.currency},
            )
        )

    # Signal 3: Rapid booking velocity (>=3 bookings by this user in last 24h)
    booking_created = _ensure_utc(booking.created_at) or datetime.now(timezone.utc)
    velocity_cutoff = booking_created - timedelta(hours=24)
    recent_bookings_count = (
        db.query(Booking)
        .filter(
            Booking.user_id == booking.user_id,
            Booking.id != booking.id,
            Booking.created_at >= velocity_cutoff,
            Booking.created_at <= booking_created,
        )
        .count()
    )
    if recent_bookings_count >= 3:
        signals.append(
            FraudSignal(
                code="RAPID_BOOKING_VELOCITY",
                weight=25,
                observed_value=f"{recent_bookings_count} bookings in 24h",
                threshold=">= 3 bookings in 24h",
                description=f"User created {recent_bookings_count} other bookings within 24 hours",
                metadata={"recent_bookings_count": recent_bookings_count},
            )
        )

    # Signal 4: Frequent refund activity
    user_refunds_count = (
        db.query(Refund)
        .join(Booking, Refund.booking_id == Booking.id)
        .filter(
            Booking.user_id == booking.user_id,
            Refund.status.in_([RefundStatus.COMPLETED, RefundStatus.APPROVED]),
        )
        .count()
    )
    user_total_bookings = (
        db.query(Booking)
        .filter(Booking.user_id == booking.user_id)
        .count()
    )
    if user_refunds_count >= 2:
        signals.append(
            FraudSignal(
                code="FREQUENT_REFUND_ACTIVITY",
                weight=20,
                observed_value=f"{user_refunds_count} refunds",
                threshold=">= 2 completed refunds",
                description=f"User has {user_refunds_count} prior completed/approved refunds",
                metadata={"user_refunds_count": user_refunds_count},
            )
        )
    elif user_total_bookings >= 2 and (user_refunds_count / user_total_bookings) >= 0.5:
        signals.append(
            FraudSignal(
                code="FREQUENT_REFUND_ACTIVITY",
                weight=20,
                observed_value=f"{user_refunds_count}/{user_total_bookings} bookings refunded",
                threshold=">= 50% refund ratio",
                description=f"User has elevated refund ratio ({user_refunds_count}/{user_total_bookings})",
                metadata={"refund_ratio": user_refunds_count / user_total_bookings},
            )
        )

    # Signal 5: New account high exposure
    if booking.user and booking.user.created_at:
        user_created = _ensure_utc(booking.user.created_at)
        account_age = booking_created - user_created
        hours_old = round(account_age.total_seconds() / 3600.0, 1)
        if account_age < timedelta(hours=24) and total_val >= Decimal("1000.00"):
            signals.append(
                FraudSignal(
                    code="NEW_ACCOUNT_HIGH_EXPOSURE",
                    weight=20,
                    observed_value=f"{hours_old}h old, {total_val} {booking.currency}",
                    threshold="< 24h old and >= 1000.00",
                    description=f"New user account ({hours_old}h old) with booking total {total_val} {booking.currency}",
                    metadata={"account_age_hours": hours_old, "total_amount": float(total_val)},
                )
            )

    # Signal 6: Large party size (>=4 passenger seats in single booking)
    passenger_count = len(booking.items)
    if passenger_count >= 4:
        signals.append(
            FraudSignal(
                code="LARGE_PARTY_SIZE",
                weight=15,
                observed_value=f"{passenger_count} passengers",
                threshold=">= 4 passengers",
                description=f"Large group booking with {passenger_count} passengers",
                metadata={"passenger_count": passenger_count},
            )
        )

    # Signal 7: Duplicate passenger names in single booking
    passenger_names = [item.passenger_name.strip().lower() for item in booking.items if item.passenger_name]
    if len(passenger_names) > len(set(passenger_names)):
        signals.append(
            FraudSignal(
                code="DUPLICATE_PASSENGER_NAMES",
                weight=30,
                observed_value=f"{len(passenger_names)} names, {len(set(passenger_names))} unique",
                threshold="repeated names present",
                description="Duplicate passenger names detected across seats in booking",
                metadata={"raw_names": passenger_names},
            )
        )

    # Signal 8: Excessive booking changes (>= 2 prior modifications)
    changes_count = len(booking.changes) if booking.changes else 0
    if changes_count >= 2:
        signals.append(
            FraudSignal(
                code="FREQUENT_BOOKING_CHANGES",
                weight=15,
                observed_value=f"{changes_count} changes",
                threshold=">= 2 booking changes",
                description=f"Booking has undergone {changes_count} prior modifications",
                metadata={"changes_count": changes_count},
            )
        )

    # Compute deterministic aggregate risk score
    raw_score = sum(s.weight for s in signals)
    risk_score = min(100, max(0, raw_score))

    # Map score to risk level and decision
    if risk_score < 30:
        risk_level = RiskLevel.LOW
        decision = FraudDecision.ALLOW
        summary = f"Low risk evaluation (score: {risk_score}/100). No significant fraud indicators detected. Recommended decision: ALLOW."
    elif risk_score < 70:
        risk_level = RiskLevel.MEDIUM
        decision = FraudDecision.REVIEW
        summary = f"Medium risk evaluation (score: {risk_score}/100). Triggered {len(signals)} risk signal(s). Recommended decision: REVIEW."
    else:
        risk_level = RiskLevel.HIGH
        decision = FraudDecision.BLOCK
        summary = f"High risk evaluation (score: {risk_score}/100). Severe risk indicators detected. Recommended decision: BLOCK."

    evaluator_name = source or EVALUATOR_VERSION

    evaluation_record = FraudEvaluation(
        booking_id=booking.id,
        user_id=booking.user_id,
        risk_score=risk_score,
        risk_level=risk_level,
        decision=decision,
        reasons={
            "signals": [s.model_dump() for s in signals],
            "summary": summary,
        },
        evaluator=evaluator_name,
        idempotency_key=idempotency_key,
    )
    db.add(evaluation_record)
    db.flush()

    # Record in audit log
    audit_actor_id = current_user.id if current_user else booking.user_id
    audit_log = AuditLog(
        user_id=audit_actor_id,
        action="FRAUD_EVALUATION",
        entity_type="booking",
        entity_id=booking.id,
        old_values=None,
        new_values={
            "evaluation_id": str(evaluation_record.id),
            "risk_score": risk_score,
            "risk_level": risk_level.value,
            "decision": decision.value,
            "evaluator": evaluator_name,
            "idempotency_key": idempotency_key,
            "signal_codes": [s.code for s in signals],
        },
    )
    db.add(audit_log)
    db.commit()
    db.refresh(evaluation_record)

    return _format_evaluation_response(evaluation_record, booking.booking_reference)


def get_latest_fraud_evaluation(db: Session, booking_id: UUID) -> FraudEvaluationResponse:
    """Retrieve the most recent fraud evaluation for a booking."""
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking with ID '{booking_id}' not found",
        )

    evaluation = (
        db.query(FraudEvaluation)
        .filter(FraudEvaluation.booking_id == booking_id)
        .order_by(desc(FraudEvaluation.evaluated_at))
        .first()
    )
    if not evaluation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No fraud evaluations found for booking '{booking_id}'",
        )

    return _format_evaluation_response(evaluation, booking.booking_reference)


def list_fraud_evaluations(db: Session, booking_id: UUID) -> FraudEvaluationListResponse:
    """Retrieve all historical fraud evaluations for a booking in reverse chronological order."""
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking with ID '{booking_id}' not found",
        )

    evaluations = (
        db.query(FraudEvaluation)
        .filter(FraudEvaluation.booking_id == booking_id)
        .order_by(desc(FraudEvaluation.evaluated_at))
        .all()
    )

    formatted = [_format_evaluation_response(e, booking.booking_reference) for e in evaluations]
    return FraudEvaluationListResponse(
        booking_id=booking.id,
        booking_reference=booking.booking_reference,
        total_evaluations=len(formatted),
        evaluations=formatted,
    )


def get_fraud_evaluation_by_id(db: Session, evaluation_id: UUID) -> FraudEvaluationResponse:
    """Retrieve a single fraud evaluation by its unique ID."""
    evaluation = (
        db.query(FraudEvaluation)
        .options(selectinload(FraudEvaluation.booking))
        .filter(FraudEvaluation.id == evaluation_id)
        .first()
    )
    if not evaluation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fraud evaluation with ID '{evaluation_id}' not found",
        )
    return _format_evaluation_response(evaluation)


def list_all_fraud_evaluations(
    db: Session,
    risk_level: Optional[RiskLevel] = None,
    decision: Optional[FraudDecision] = None,
    booking_id: Optional[UUID] = None,
    user_id: Optional[UUID] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    evaluator: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedFraudEvaluationResponse:
    """
    List all fraud evaluations across the system with database-level filtering and pagination.
    Designed for operational fraud queues, n8n polling nodes, and human review dashboards.
    """
    query = db.query(FraudEvaluation).options(selectinload(FraudEvaluation.booking))

    if risk_level:
        query = query.filter(FraudEvaluation.risk_level == risk_level)
    if decision:
        query = query.filter(FraudEvaluation.decision == decision)
    if booking_id:
        query = query.filter(FraudEvaluation.booking_id == booking_id)
    if user_id:
        query = query.filter(FraudEvaluation.user_id == user_id)
    if from_date:
        query = query.filter(FraudEvaluation.evaluated_at >= _ensure_utc(from_date))
    if to_date:
        query = query.filter(FraudEvaluation.evaluated_at <= _ensure_utc(to_date))
    if evaluator:
        query = query.filter(FraudEvaluation.evaluator == evaluator)

    total = query.count()
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    offset = (page - 1) * page_size

    records = (
        query.order_by(desc(FraudEvaluation.evaluated_at))
        .offset(offset)
        .limit(page_size)
        .all()
    )

    items = [_format_evaluation_response(r) for r in records]

    return PaginatedFraudEvaluationResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
