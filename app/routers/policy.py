from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_ops_or_admin, get_current_user
from app.models.enums import (
    PolicyDocumentStatus,
    PolicyIngestionStatus,
    PolicyType,
)
from app.models.user import User
from app.schemas.policy import (
    BookingRAGContextResponse,
    PaginatedPolicyDocumentResponse,
    PolicyChangeDetectionRequest,
    PolicyChangeDetectionResponse,
    PolicyDocumentCreate,
    PolicyDocumentResponse,
    PolicyDocumentUpdate,
)
from app.services.policy_service import (
    detect_policy_document_changes,
    get_booking_rag_context,
    get_policy_document_by_id,
    list_policy_documents,
    register_policy_document,
    update_policy_document,
)

router = APIRouter(prefix="/policy", tags=["Policy Knowledge Base & RAG"])


@router.post(
    "/documents",
    response_model=PolicyDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register Policy Document Metadata",
    description="Registers a new policy document metadata entry in the FMS knowledge base. Computes SHA-256 hash, enforces version uniqueness, and optionally supersedes older active versions.",
)
def register_document(
    payload: PolicyDocumentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return register_policy_document(db=db, payload=payload, current_user=current_user)


@router.get(
    "/documents",
    response_model=PaginatedPolicyDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="List Registered Policy Documents",
    description="Lists policy document registry records with filtering by policy_type, status, ingestion_status, and search text.",
)
def list_documents(
    policy_type: Optional[PolicyType] = Query(None, description="Filter by policy type (e.g. CANCELLATION, REFUND)"),
    status_filter: Optional[PolicyDocumentStatus] = Query(None, alias="status", description="Filter by lifecycle status (ACTIVE, INACTIVE, SUPERSEDED, ARCHIVED)"),
    ingestion_status: Optional[PolicyIngestionStatus] = Query(None, description="Filter by n8n ingestion status (PENDING, PROCESSING, COMPLETED, FAILED, SKIPPED)"),
    search: Optional[str] = Query(None, description="Search document name or source"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return list_policy_documents(
        db=db,
        policy_type=policy_type,
        status_filter=status_filter,
        ingestion_status=ingestion_status,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/documents/{document_id}",
    response_model=PolicyDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Policy Document Metadata",
    description="Retrieves metadata details for a specific registered policy document by UUID.",
)
def get_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return get_policy_document_by_id(db=db, document_id=document_id)


@router.patch(
    "/documents/{document_id}",
    response_model=PolicyDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Policy Document Metadata or Ingestion State",
    description="Updates document status (e.g. ACTIVE -> ARCHIVED) or ingestion status (e.g. PENDING -> COMPLETED after Pinecone embedding).",
)
def update_document(
    document_id: UUID,
    payload: PolicyDocumentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return update_policy_document(db=db, document_id=document_id, payload=payload, current_user=current_user)


@router.post(
    "/documents/detect-changes",
    response_model=PolicyChangeDetectionResponse,
    status_code=status.HTTP_200_OK,
    summary="Detect Policy Document Content Changes",
    description="Compares incoming document candidates against currently active registered documents via SHA-256 content hashes. Returns UNCHANGED, CONTENT_CHANGED, or NEW_DOCUMENT, signaling to n8n whether Pinecone re-embedding is necessary.",
)
def detect_changes(
    payload: PolicyChangeDetectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_ops_or_admin),
):
    return detect_policy_document_changes(db=db, payload=payload)


@router.get(
    "/context/booking/{booking_id}",
    response_model=BookingRAGContextResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Authoritative Booking RAG Context",
    description="Retrieves authoritative booking, flight, passenger, items, and immutable fare-rule snapshot facts. Accessible to booking owner (passenger) and Ops/Admins.",
)
def get_context_for_booking(
    booking_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_booking_rag_context(db=db, booking_id=booking_id, current_user=current_user)
