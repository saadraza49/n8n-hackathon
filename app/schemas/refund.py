from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BookingStatus, RefundReason, RefundStatus, RefundType


class BookingCancellationRequest(BaseModel):
    item_ids: Optional[List[UUID]] = Field(
        None,
        description="Optional list of specific booking item IDs to cancel (partial cancellation). If omitted, cancels all active items.",
    )
    reason: Optional[RefundReason] = Field(
        RefundReason.CUSTOMER_CANCELLATION,
        description="Reason for cancellation",
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=255,
        description="Client idempotency key to prevent duplicate cancellation/refunds",
    )


class RefundResponse(BaseModel):
    id: UUID
    booking_id: UUID
    booking_item_id: Optional[UUID] = None
    amount: Decimal
    currency: str
    refund_type: RefundType
    status: RefundStatus
    reason: RefundReason
    idempotency_key: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CancellationSummaryResponse(BaseModel):
    booking_id: UUID
    status: BookingStatus
    cancelled_items_count: int
    remaining_items_count: int
    refunds: List[RefundResponse] = []
    total_refund_amount: Decimal
    currency: str
    message: str
