from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.policy import BookingRAGContextResponse
from app.services.policy_service import get_booking_rag_context

router = APIRouter(prefix="/rag", tags=["Policy Knowledge Base & RAG"])


@router.get(
    "/bookings/{booking_id}/context",
    response_model=BookingRAGContextResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Authoritative Booking RAG Context (RAG Endpoint)",
    description="Dedicated RAG agent endpoint returning authoritative booking, flight, item, and purchased fare-rule snapshot facts. Accessible to booking owner and Ops/Admins.",
)
def get_rag_booking_context(
    booking_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_booking_rag_context(db=db, booking_id=booking_id, current_user=current_user)
