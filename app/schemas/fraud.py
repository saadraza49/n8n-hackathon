from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FraudDecision, RiskLevel


class FraudSignal(BaseModel):
    code: str = Field(..., description="Machine-readable signal code")
    weight: int = Field(..., description="Weight / risk points contributed by this signal")
    severity: str = Field("MEDIUM", description="Severity level: LOW, MEDIUM, HIGH, CRITICAL")
    description: str = Field(..., description="Human-readable explanation of why this signal fired")
    observed_value: Optional[str] = Field(None, description="Observed metric value (e.g. '5200.00 USD')")
    threshold: Optional[str] = Field(None, description="Configured rule threshold (e.g. '>= 5000.00 USD')")
    evidence: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Structured evidence context")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Structured signal metadata")


class RiskSignalResponse(BaseModel):
    id: UUID
    evaluation_id: UUID
    signal_code: str
    severity: str
    score_contribution: int
    description: str
    evidence: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FraudReasonDetails(BaseModel):
    signals: List[FraudSignal] = Field(default_factory=list, description="List of triggered risk signals")
    summary: str = Field(..., description="Executive summary of the fraud/risk assessment")


class FraudEvaluationRequest(BaseModel):
    booking_id: Optional[UUID] = Field(None, description="Booking ID to evaluate (if calling standalone /fraud/evaluate or /risk/evaluate)")
    source: Optional[str] = Field(None, max_length=100, description="Evaluator, agent, or workflow calling this check")
    force_re_evaluate: bool = Field(
        False,
        description="If True, forces computing a new evaluation and persisting it. If False, returns latest existing evaluation if available.",
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=255,
        description="Client or workflow idempotency key to prevent duplicate evaluations",
    )


class FraudEvaluationResponse(BaseModel):
    id: UUID
    booking_id: UUID
    booking_reference: Optional[str] = None
    user_id: Optional[UUID] = None
    risk_score: int = Field(..., ge=0, le=100, description="Calculated risk score between 0 and 100")
    risk_level: RiskLevel
    decision: FraudDecision
    reasons: FraudReasonDetails
    evaluator: str
    idempotency_key: Optional[str] = None
    evaluated_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FraudEvaluationListResponse(BaseModel):
    booking_id: UUID
    booking_reference: Optional[str] = None
    total_evaluations: int
    evaluations: List[FraudEvaluationResponse]


class PaginatedFraudEvaluationResponse(BaseModel):
    items: List[FraudEvaluationResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
