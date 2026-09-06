from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_ops_or_admin
from app.models.enums import FraudDecision, RiskLevel
from app.models.user import User
from app.schemas.fraud import (
    FraudEvaluationRequest,
    FraudEvaluationResponse,
    PaginatedFraudEvaluationResponse,
    RiskSignalResponse,
)
from app.services.fraud_service import (
    evaluate_booking_risk,
    get_fraud_evaluation_by_id,
    get_risk_signals_for_evaluation,
    list_all_fraud_evaluations,
)

router = APIRouter(prefix="/fraud", tags=["Fraud & Risk"])
risk_router = APIRouter(prefix="/risk", tags=["Fraud & Risk"])


# ============================================================================
# /fraud ROUTER IMPLEMENTATION
# ============================================================================

@router.get(
    "/evaluations",
    response_model=PaginatedFraudEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="List Operational Fraud Evaluations",
    description="Retrieve paginated list of fraud evaluations across all bookings with database-level filtering. Restricted to Ops Agents and Super Admins.",
)
def list_evaluations(
    risk_level: Optional[RiskLevel] = Query(None, description="Filter by risk severity level (LOW, MEDIUM, HIGH, CRITICAL)"),
    decision: Optional[FraudDecision] = Query(None, description="Filter by recommended decision (ALLOW, REVIEW, BLOCK)"),
    booking_id: Optional[UUID] = Query(None, description="Filter by specific booking ID"),
    user_id: Optional[UUID] = Query(None, description="Filter by specific user ID"),
    from_date: Optional[datetime] = Query(None, description="Filter evaluations evaluated on or after this ISO datetime"),
    to_date: Optional[datetime] = Query(None, description="Filter evaluations evaluated on or before this ISO datetime"),
    evaluator: Optional[str] = Query(None, description="Filter by evaluator identifier (e.g. 'RULE_ENGINE_V1')"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return list_all_fraud_evaluations(
        db=db,
        risk_level=risk_level,
        decision=decision,
        booking_id=booking_id,
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        evaluator=evaluator,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/evaluations/{evaluation_id}",
    response_model=FraudEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Fraud Evaluation Details",
    description="Retrieve detailed information, explainable signals, and risk score for a specific fraud evaluation record. Restricted to Ops and Admins.",
)
def get_evaluation(
    evaluation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return get_fraud_evaluation_by_id(db=db, evaluation_id=evaluation_id)


@router.get(
    "/evaluations/{evaluation_id}/signals",
    response_model=List[RiskSignalResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Child Risk Signals for Evaluation",
    description="Retrieve individual machine-readable risk signals stored in the child risk_signals table. Restricted to Ops and Admins.",
)
def get_signals(
    evaluation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return get_risk_signals_for_evaluation(db=db, evaluation_id=evaluation_id)


@router.post(
    "/evaluate",
    response_model=FraudEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Evaluate Booking Risk (Operational / Agent / n8n)",
    description="Trigger or fetch risk evaluation for a booking via JSON payload. Supports idempotency key and source tracking. Restricted to Ops and Admins.",
)
def evaluate_booking(
    payload: FraudEvaluationRequest,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    if not payload.booking_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Field 'booking_id' is required in request body",
        )

    effective_idempotency_key = payload.idempotency_key or idempotency_key_header

    return evaluate_booking_risk(
        db=db,
        booking_id=payload.booking_id,
        current_user=current_user,
        source=payload.source,
        force_re_evaluate=payload.force_re_evaluate,
        idempotency_key=effective_idempotency_key,
    )


# ============================================================================
# /risk ROUTER IMPLEMENTATION (REST ALIAS FOR RISK DOMAIN)
# ============================================================================

@risk_router.post(
    "/evaluate/{booking_id}",
    response_model=FraudEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Evaluate Booking Risk (/risk endpoint)",
    description="Deterministic risk evaluation for a booking. Supports Idempotency-Key. Restricted to Ops and Admins.",
)
def risk_evaluate_booking(
    booking_id: UUID,
    payload: Optional[FraudEvaluationRequest] = None,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    source = payload.source if payload else None
    force_re_evaluate = payload.force_re_evaluate if payload else False
    idempotency_key = (payload.idempotency_key if payload else None) or idempotency_key_header
    return evaluate_booking_risk(
        db=db,
        booking_id=booking_id,
        current_user=current_user,
        source=source,
        force_re_evaluate=force_re_evaluate,
        idempotency_key=idempotency_key,
    )


@risk_router.get(
    "/evaluations",
    response_model=PaginatedFraudEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="List Risk Evaluations (/risk endpoint)",
    description="Retrieve paginated list of risk evaluations with database-level filtering. Restricted to Ops and Admins.",
)
def risk_list_evaluations(
    risk_level: Optional[RiskLevel] = Query(None, description="Filter by risk severity level (LOW, MEDIUM, HIGH, CRITICAL)"),
    decision: Optional[FraudDecision] = Query(None, description="Filter by recommended decision (ALLOW, REVIEW, BLOCK)"),
    booking_id: Optional[UUID] = Query(None, description="Filter by specific booking ID"),
    user_id: Optional[UUID] = Query(None, description="Filter by specific user ID"),
    from_date: Optional[datetime] = Query(None, description="Filter evaluations on or after this ISO datetime"),
    to_date: Optional[datetime] = Query(None, description="Filter evaluations on or before this ISO datetime"),
    evaluator: Optional[str] = Query(None, description="Filter by evaluator identifier"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return list_all_fraud_evaluations(
        db=db,
        risk_level=risk_level,
        decision=decision,
        booking_id=booking_id,
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        evaluator=evaluator,
        page=page,
        page_size=page_size,
    )


@risk_router.get(
    "/evaluations/{evaluation_id}",
    response_model=FraudEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Risk Evaluation Details (/risk endpoint)",
    description="Retrieve details for a specific risk evaluation record. Restricted to Ops and Admins.",
)
def risk_get_evaluation(
    evaluation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return get_fraud_evaluation_by_id(db=db, evaluation_id=evaluation_id)


@risk_router.get(
    "/evaluations/{evaluation_id}/signals",
    response_model=List[RiskSignalResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Risk Signals for Evaluation (/risk endpoint)",
    description="Retrieve individual machine-readable risk signals stored in child table. Restricted to Ops and Admins.",
)
def risk_get_signals(
    evaluation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return get_risk_signals_for_evaluation(db=db, evaluation_id=evaluation_id)
