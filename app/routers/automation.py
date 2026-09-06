from datetime import date
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_ops_or_admin
from app.models.user import User
from app.schemas.notification import (
    NotificationClaimRequest,
    NotificationResponse,
    NotificationStatusUpdateRequest,
)
from app.schemas.price_history import PriceHistoryResponse
from app.schemas.reporting import (
    DailyOperationalSummaryResponse,
    EligibleCheckinReminderResponse,
)
from app.services.automation_service import (
    claim_pending_notifications,
    get_eligible_checkin_reminders,
    get_flight_operational_metrics,
    get_price_history_for_flight,
    update_notification_status,
)

router = APIRouter(prefix="/automation", tags=["Automation & Reporting"])


@router.get(
    "/reports/operational",
    response_model=DailyOperationalSummaryResponse,
    summary="Daily & Weekly Operational Report Metrics",
    description="Aggregates load factor %, gross revenue, total refunds, and net revenue across flights.",
)
def get_operational_report(
    start_date: Optional[date] = Query(None, description="Start departure date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End departure date (YYYY-MM-DD)"),
    current_user: User = Depends(get_current_ops_or_admin),
    db: Session = Depends(get_db),
):
    return get_flight_operational_metrics(db=db, start_date=start_date, end_date=end_date)


@router.get(
    "/reminders/eligible",
    response_model=List[EligibleCheckinReminderResponse],
    summary="List Passengers Eligible for Check-in Reminders",
    description="Identifies confirmed bookings on scheduled flights eligible for reminders. Automatically excludes cancelled flights.",
)
def list_eligible_reminders(
    max_hours_ahead: int = Query(24, ge=1, le=168, description="Hours ahead of departure"),
    current_user: User = Depends(get_current_ops_or_admin),
    db: Session = Depends(get_db),
):
    return get_eligible_checkin_reminders(db=db, max_hours_ahead=max_hours_ahead)


@router.get(
    "/price-history/{flight_id}",
    response_model=List[PriceHistoryResponse],
    summary="Flight Price Movement History",
    description="Retrieves persisted historical price movements and drops for a specific flight.",
)
def list_flight_price_history(
    flight_id: UUID,
    current_user: User = Depends(get_current_ops_or_admin),
    db: Session = Depends(get_db),
):
    return get_price_history_for_flight(db=db, flight_id=flight_id)


@router.post(
    "/notifications/claim",
    response_model=List[NotificationResponse],
    summary="Claim Pending Notifications (Worker Emulation)",
    description="Simulates n8n worker claiming pending notifications using row-level locking.",
)
def claim_notifications(
    payload: NotificationClaimRequest,
    current_user: User = Depends(get_current_ops_or_admin),
    db: Session = Depends(get_db),
):
    return claim_pending_notifications(db=db, batch_size=payload.batch_size)


@router.patch(
    "/notifications/{notification_id}",
    response_model=NotificationResponse,
    summary="Update Notification Delivery Status",
    description="Updates notification state to SENT, FAILED (with retry scheduling), or CANCELLED.",
)
def update_notification(
    notification_id: UUID,
    payload: NotificationStatusUpdateRequest,
    current_user: User = Depends(get_current_ops_or_admin),
    db: Session = Depends(get_db),
):
    return update_notification_status(db=db, notification_id=notification_id, payload=payload)
