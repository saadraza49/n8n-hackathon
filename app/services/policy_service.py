from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import desc, func
from sqlalchemy.orm import Session, selectinload

from app.models.audit_log import AuditLog
from app.models.booking import Booking, BookingItem
from app.models.enums import (
    BookingItemStatus,
    BookingStatus,
    FareType,
    FlightClassType,
    PolicyDocumentStatus,
    PolicyIngestionStatus,
    PolicyType,
    UserRole,
)
from app.models.fare_rule import FareRule
from app.models.flight import Flight
from app.models.policy import PolicyDocument
from app.models.user import User
from app.schemas.policy import (
    BookingFact,
    BookingItemFact,
    BookingPolicyContextFact,
    BookingRAGContextResponse,
    FareRuleSnapshotFact,
    FlightFact,
    PaginatedPolicyDocumentResponse,
    PassengerFact,
    PolicyChangeDetectionRequest,
    PolicyChangeDetectionResponse,
    PolicyDocumentChangeResult,
    PolicyDocumentCreate,
    PolicyDocumentResponse,
    PolicyDocumentUpdate,
)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Helper to ensure datetime is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ============================================================================
# 1. POLICY DOCUMENT REGISTRY SERVICE
# ============================================================================

def register_policy_document(
    db: Session,
    payload: PolicyDocumentCreate,
    current_user: User,
) -> PolicyDocumentResponse:
    """
    Registers a new policy document in the knowledge-base registry.
    Calculates SHA-256 hash from content if provided, enforces unique (document_name, version),
    optionally supersedes previous active versions, and logs an audit record.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Compute SHA-256 hash & content length
    content_length: Optional[int] = None
    if payload.content is not None:
        encoded_content = payload.content.encode("utf-8")
        computed_hash = hashlib.sha256(encoded_content).hexdigest()
        content_length = len(encoded_content)
    elif payload.document_hash is not None:
        computed_hash = payload.document_hash.strip().lower()
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'content' or 'document_hash' must be provided for policy document registration.",
        )

    # 2. Check for duplicate document_name + version
    existing_version = (
        db.query(PolicyDocument)
        .filter(
            PolicyDocument.document_name == payload.document_name,
            PolicyDocument.version == payload.version,
        )
        .first()
    )
    if existing_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Policy document '{payload.document_name}' version '{payload.version}' is already registered (id: {existing_version.id}).",
        )

    # 3. Handle superseding previous active version
    if payload.supersede_previous_active:
        active_prior_docs = (
            db.query(PolicyDocument)
            .filter(
                PolicyDocument.document_name == payload.document_name,
                PolicyDocument.status == PolicyDocumentStatus.ACTIVE,
            )
            .all()
        )
        for prior_doc in active_prior_docs:
            prior_doc.status = PolicyDocumentStatus.SUPERSEDED
            prior_doc.effective_until = payload.effective_from or now_utc

            audit_supersede = AuditLog(
                user_id=current_user.id,
                action="POLICY_DOCUMENT_SUPERSEDED",
                entity_type="PolicyDocument",
                entity_id=prior_doc.id,
                old_values={"status": PolicyDocumentStatus.ACTIVE.value},
                new_values={
                    "status": PolicyDocumentStatus.SUPERSEDED.value,
                    "superseded_by_version": payload.version,
                    "superseded_at": now_utc.isoformat(),
                },
            )
            db.add(audit_supersede)

    # 4. Create new PolicyDocument record
    policy_doc = PolicyDocument(
        document_name=payload.document_name,
        policy_type=payload.policy_type,
        version=payload.version,
        source=payload.source,
        source_url=payload.source_url,
        document_hash=computed_hash,
        status=PolicyDocumentStatus.ACTIVE,
        ingestion_status=PolicyIngestionStatus.PENDING,
        effective_from=_ensure_utc(payload.effective_from) or now_utc,
        effective_until=_ensure_utc(payload.effective_until),
        content_length=content_length,
        chunk_count=payload.chunk_count or 0,
        metadata_info=payload.metadata_info or {},
        created_by=current_user.id,
    )
    db.add(policy_doc)
    db.flush()

    # 5. Audit Log Entry
    audit_entry = AuditLog(
        user_id=current_user.id,
        action="POLICY_DOCUMENT_REGISTERED",
        entity_type="PolicyDocument",
        entity_id=policy_doc.id,
        new_values={
            "document_name": policy_doc.document_name,
            "policy_type": policy_doc.policy_type.value,
            "version": policy_doc.version,
            "source": policy_doc.source,
            "document_hash": policy_doc.document_hash,
            "status": policy_doc.status.value,
            "ingestion_status": policy_doc.ingestion_status.value,
            "effective_from": policy_doc.effective_from.isoformat(),
        },
    )
    db.add(audit_entry)
    db.commit()
    db.refresh(policy_doc)

    return PolicyDocumentResponse.model_validate(policy_doc)


def update_policy_document(
    db: Session,
    document_id: UUID,
    payload: PolicyDocumentUpdate,
    current_user: User,
) -> PolicyDocumentResponse:
    """Updates status, ingestion state, or metadata for a registered policy document."""
    doc = db.query(PolicyDocument).filter(PolicyDocument.id == document_id).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy document '{document_id}' not found.",
        )

    old_values = {
        "status": doc.status.value,
        "ingestion_status": doc.ingestion_status.value,
        "chunk_count": doc.chunk_count,
        "source_url": doc.source_url,
    }

    action = "POLICY_DOCUMENT_UPDATED"
    now_utc = datetime.now(timezone.utc)

    if payload.status is not None:
        doc.status = payload.status
    if payload.ingestion_status is not None:
        doc.ingestion_status = payload.ingestion_status
        action = "POLICY_INGESTION_STATE_CHANGED"
        if payload.ingestion_status == PolicyIngestionStatus.COMPLETED:
            doc.retrieved_at = now_utc
    if payload.source_url is not None:
        doc.source_url = payload.source_url
    if payload.effective_until is not None:
        doc.effective_until = _ensure_utc(payload.effective_until)
    if payload.chunk_count is not None:
        doc.chunk_count = payload.chunk_count
    if payload.metadata_info is not None:
        doc.metadata_info = {**doc.metadata_info, **payload.metadata_info}

    new_values = {
        "status": doc.status.value,
        "ingestion_status": doc.ingestion_status.value,
        "chunk_count": doc.chunk_count,
        "source_url": doc.source_url,
    }

    audit_entry = AuditLog(
        user_id=current_user.id,
        action=action,
        entity_type="PolicyDocument",
        entity_id=doc.id,
        old_values=old_values,
        new_values=new_values,
    )
    db.add(audit_entry)
    db.commit()
    db.refresh(doc)

    return PolicyDocumentResponse.model_validate(doc)


def get_policy_document_by_id(db: Session, document_id: UUID) -> PolicyDocumentResponse:
    """Retrieves specific policy document metadata by UUID."""
    doc = db.query(PolicyDocument).filter(PolicyDocument.id == document_id).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy document '{document_id}' not found.",
        )
    return PolicyDocumentResponse.model_validate(doc)


def list_policy_documents(
    db: Session,
    policy_type: Optional[PolicyType] = None,
    status_filter: Optional[PolicyDocumentStatus] = None,
    ingestion_status: Optional[PolicyIngestionStatus] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedPolicyDocumentResponse:
    """Lists policy documents with filtering and pagination for n8n or admin dashboards."""
    query = db.query(PolicyDocument)

    if policy_type is not None:
        query = query.filter(PolicyDocument.policy_type == policy_type)
    if status_filter is not None:
        query = query.filter(PolicyDocument.status == status_filter)
    if ingestion_status is not None:
        query = query.filter(PolicyDocument.ingestion_status == ingestion_status)
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            (PolicyDocument.document_name.ilike(term)) | (PolicyDocument.source.ilike(term))
        )

    total = query.count()
    items = (
        query.order_by(desc(PolicyDocument.effective_from), desc(PolicyDocument.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return PaginatedPolicyDocumentResponse(
        items=[PolicyDocumentResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# ============================================================================
# 2. CHANGE DETECTION SERVICE (FOR N8N INGESTION AGENTS)
# ============================================================================

def detect_policy_document_changes(
    db: Session,
    payload: PolicyChangeDetectionRequest,
) -> PolicyChangeDetectionResponse:
    """
    Compares candidate policy documents against active documents in PostgreSQL.
    Determines whether documents are NEW, CHANGED, or UNCHANGED, allowing n8n
    to avoid re-embedding unchanged documents in Pinecone.
    """
    results: List[PolicyDocumentChangeResult] = []
    requires_embedding_count = 0

    for item in payload.documents:
        # 1. Resolve hash
        if item.content is not None:
            supplied_hash = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        elif item.document_hash is not None:
            supplied_hash = item.document_hash.strip().lower()
        else:
            supplied_hash = ""

        # 2. Query active registered document
        active_doc = (
            db.query(PolicyDocument)
            .filter(
                PolicyDocument.document_name == item.document_name,
                PolicyDocument.status == PolicyDocumentStatus.ACTIVE,
            )
            .order_by(desc(PolicyDocument.created_at))
            .first()
        )

        if not active_doc:
            # Check if any document exists at all under this name
            any_doc = (
                db.query(PolicyDocument)
                .filter(PolicyDocument.document_name == item.document_name)
                .order_by(desc(PolicyDocument.created_at))
                .first()
            )
            if any_doc:
                status_res = "SUPERSEDED"
                req_emb = True
                reason = f"No ACTIVE version found; latest existing version is {any_doc.version} with status {any_doc.status.value}."
                doc_id = any_doc.id
                ver = any_doc.version
                doc_status = any_doc.status
                ingest_status = any_doc.ingestion_status
            else:
                status_res = "NEW_DOCUMENT"
                req_emb = True
                reason = "Document not found in registry. Requires initial ingestion and embedding."
                doc_id = None
                ver = None
                doc_status = None
                ingest_status = None
        else:
            doc_id = active_doc.id
            ver = active_doc.version
            doc_status = active_doc.status
            ingest_status = active_doc.ingestion_status

            if active_doc.document_hash == supplied_hash:
                status_res = "UNCHANGED"
                req_emb = False
                reason = f"Content hash matches active version {active_doc.version}. Re-embedding skipped."
            else:
                status_res = "CONTENT_CHANGED"
                req_emb = True
                reason = f"Content hash differs from active version {active_doc.version}. Re-embedding required."

        if req_emb:
            requires_embedding_count += 1

        results.append(
            PolicyDocumentChangeResult(
                document_name=item.document_name,
                version=item.version or ver,
                supplied_hash=supplied_hash,
                change_status=status_res,
                requires_embedding=req_emb,
                existing_document_id=doc_id,
                existing_version=ver,
                existing_status=doc_status,
                existing_ingestion_status=ingest_status,
                reason=reason,
            )
        )

    return PolicyChangeDetectionResponse(
        results=results,
        total_evaluated=len(results),
        requires_embedding_count=requires_embedding_count,
    )


# ============================================================================
# 3. AUTHORITATIVE BOOKING RAG CONTEXT SERVICE
# ============================================================================

def get_booking_rag_context(
    db: Session,
    booking_id: UUID,
    current_user: User,
) -> BookingRAGContextResponse:
    """
    Retrieves authoritative booking, passenger, flight, item, and fare-rule snapshot facts.
    Guarantees:
    - Fare Rule Authority: Prefers immutable fare_rule_snapshot stored at purchase time.
    - Zero Hallucination: Provides exact refundability, cutoff deadlines, and departure hours.
    - RBAC & IDOR Safety: Passengers may only query their own bookings.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Fetch Booking with full hierarchy
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .options(
            selectinload(Booking.user),
            selectinload(Booking.flight),
            selectinload(Booking.items).selectinload(BookingItem.seat),
        )
        .first()
    )
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking '{booking_id}' not found.",
        )

    # 2. RBAC & IDOR Verification
    if current_user.role == UserRole.PASSENGER and booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view RAG context for this booking.",
        )

    # 3. Build Booking, Passenger, and Flight facts
    booking_fact = BookingFact(
        id=booking.id,
        booking_reference=booking.booking_reference,
        status=booking.status,
        total_amount=booking.total_amount,
        currency=booking.currency,
        hold_expires_at=_ensure_utc(booking.hold_expires_at),
        created_at=_ensure_utc(booking.created_at),
        updated_at=_ensure_utc(booking.updated_at),
    )

    passenger_fact = PassengerFact(
        id=booking.user.id,
        name=booking.user.name,
        email=booking.user.email,
        role=booking.user.role,
    )

    flight = booking.flight
    flight_fact = FlightFact(
        id=flight.id,
        flight_number=flight.flight_number,
        origin=flight.origin,
        destination=flight.destination,
        departure_at=_ensure_utc(flight.departure_at),
        arrival_at=_ensure_utc(flight.arrival_at),
        status=flight.status,
    )

    # 4. Resolve Item Facts and Fare Rule Snapshots
    item_facts: List[BookingItemFact] = []
    primary_rule: Optional[FareRuleSnapshotFact] = None

    for item in booking.items:
        # Determine snapshot
        if item.fare_rule_snapshot and isinstance(item.fare_rule_snapshot, dict):
            snap = item.fare_rule_snapshot
            snapshot_fact = FareRuleSnapshotFact(
                fare_rule_id=str(snap.get("fare_rule_id")) if snap.get("fare_rule_id") else None,
                fare_type=FareType(snap.get("fare_type", item.fare_type.value)),
                class_type=FlightClassType(snap.get("class_type", item.class_type.value)),
                price=Decimal(str(snap.get("price", item.price))),
                currency=str(snap.get("currency", item.currency)),
                refundable=bool(snap.get("refundable", False)),
                credit_only=bool(snap.get("credit_only", False)),
                changes_allowed=bool(snap.get("changes_allowed", False)),
                seat_selection_allowed=bool(snap.get("seat_selection_allowed", False)),
                cancellation_cutoff_minutes=int(snap.get("cancellation_cutoff_minutes", 0)),
                snapshotted_at=datetime.fromisoformat(snap["snapshotted_at"]) if "snapshotted_at" in snap else _ensure_utc(item.created_at),
            )
        else:
            # Fallback to querying fare_rules for legacy bookings created before snapshot feature
            live_rule = (
                db.query(FareRule)
                .filter(
                    FareRule.flight_id == item.flight_id,
                    FareRule.class_type == item.class_type,
                    FareRule.fare_type == item.fare_type,
                )
                .first()
            )
            snapshot_fact = FareRuleSnapshotFact(
                fare_rule_id=str(live_rule.id) if live_rule else None,
                fare_type=item.fare_type,
                class_type=item.class_type,
                price=item.price,
                currency=item.currency,
                refundable=live_rule.refundable if live_rule else False,
                credit_only=live_rule.credit_only if live_rule else False,
                changes_allowed=live_rule.changes_allowed if live_rule else False,
                seat_selection_allowed=live_rule.seat_selection_allowed if live_rule else False,
                cancellation_cutoff_minutes=live_rule.cancellation_cutoff_minutes if live_rule else 0,
                snapshotted_at=_ensure_utc(item.created_at),
            )

        if primary_rule is None and item.status == BookingItemStatus.CONFIRMED:
            primary_rule = snapshot_fact

        seat_number = item.seat.seat_number if item.seat else None
        item_facts.append(
            BookingItemFact(
                id=item.id,
                seat_id=item.seat_id,
                seat_number=seat_number,
                passenger_name=item.passenger_name,
                class_type=item.class_type,
                fare_type=item.fare_type,
                price=item.price,
                currency=item.currency,
                status=item.status,
                fare_rule_snapshot=snapshot_fact,
            )
        )

    # Fallback to first item snapshot if no confirmed item found
    if primary_rule is None and item_facts:
        primary_rule = item_facts[0].fare_rule_snapshot
    elif primary_rule is None:
        primary_rule = FareRuleSnapshotFact(
            fare_type=FareType.BASIC,
            class_type=FlightClassType.ECONOMY,
            price=Decimal("0.00"),
            currency="USD",
            refundable=False,
            credit_only=False,
            changes_allowed=False,
            seat_selection_allowed=False,
            cancellation_cutoff_minutes=0,
            snapshotted_at=now_utc,
        )

    # 5. Calculate Policy Context
    dep_utc = _ensure_utc(flight.departure_at)
    hours_until_dep = round((dep_utc - now_utc).total_seconds() / 3600.0, 2) if dep_utc else None
    cutoff_deadline = dep_utc - timedelta(minutes=primary_rule.cancellation_cutoff_minutes) if dep_utc else None
    is_before_cutoff = (now_utc <= cutoff_deadline) if cutoff_deadline else False

    is_cancellation_allowed = (
        booking.status in (BookingStatus.CONFIRMED, BookingStatus.PENDING)
        and is_before_cutoff
        and (primary_rule.refundable or primary_rule.credit_only)
    )

    if booking.status == BookingStatus.CANCELLED:
        summary = "Booking is already CANCELLED. No further changes or cancellations possible."
    elif not is_before_cutoff:
        summary = f"Cancellation deadline has passed ({cutoff_deadline.isoformat() if cutoff_deadline else 'N/A'}). Refund not permitted."
    elif primary_rule.refundable:
        summary = f"Eligible for monetary refund. Flight departs in {hours_until_dep}h; cutoff is {primary_rule.cancellation_cutoff_minutes}m prior."
    elif primary_rule.credit_only:
        summary = f"Eligible for airline travel credit refund only. Flight departs in {hours_until_dep}h."
    else:
        summary = "Non-refundable fare per authoritative purchased fare rule terms."

    policy_context_fact = BookingPolicyContextFact(
        fare_type=primary_rule.fare_type,
        refundable=primary_rule.refundable,
        credit_only=primary_rule.credit_only,
        changes_allowed=primary_rule.changes_allowed,
        seat_selection_allowed=primary_rule.seat_selection_allowed,
        cancellation_cutoff_minutes=primary_rule.cancellation_cutoff_minutes,
        cancellation_deadline=cutoff_deadline,
        hours_until_departure=hours_until_dep,
        is_before_cancellation_cutoff=is_before_cutoff,
        is_cancellation_allowed=is_cancellation_allowed,
        policy_summary=summary,
    )

    return BookingRAGContextResponse(
        booking=booking_fact,
        passenger=passenger_fact,
        flight=flight_fact,
        items=item_facts,
        policy_context=policy_context_fact,
        retrieved_at=now_utc,
    )
