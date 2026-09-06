from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import NotificationChannel, NotificationStatus, NotificationType


class NotificationBase(BaseModel):
    notification_type: NotificationType
    channel: NotificationChannel = NotificationChannel.EMAIL
    recipient: str = Field(..., min_length=3, max_length=255)
    flight_id: Optional[UUID] = None
    booking_id: Optional[UUID] = None
    deduplication_key: str = Field(..., min_length=5, max_length=255)
    payload: Optional[Dict[str, Any]] = None


class NotificationCreate(NotificationBase):
    user_id: Optional[UUID] = None
    scheduled_for: Optional[datetime] = None


class NotificationResponse(NotificationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: Optional[UUID] = None
    status: NotificationStatus
    scheduled_for: datetime
    processing_started_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    attempt_count: int
    max_retries: int
    last_error: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class NotificationClaimRequest(BaseModel):
    batch_size: int = Field(default=10, ge=1, le=100, description="Number of pending notifications to claim")


class NotificationStatusUpdateRequest(BaseModel):
    status: NotificationStatus = Field(..., description="Target status (SENT, FAILED, CANCELLED)")
    error_message: Optional[str] = Field(None, description="Error message if status is FAILED")
    next_retry_minutes: Optional[int] = Field(None, ge=1, description="Minutes until next retry attempt if FAILED")
