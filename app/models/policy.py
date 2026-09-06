import uuid
from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.enums import (
    PolicyDocumentStatus,
    PolicyIngestionStatus,
    PolicyType,
)


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    __table_args__ = (
        UniqueConstraint("document_name", "version", name="uq_policy_documents_name_version"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_name = Column(String(255), nullable=False, index=True)
    policy_type = Column(
        SAEnum(PolicyType, name="policy_type", native_enum=True, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    version = Column(String(50), nullable=False, index=True)
    source = Column(String(255), nullable=False)
    source_url = Column(String(1024), nullable=True)
    document_hash = Column(String(64), nullable=False, index=True)
    status = Column(
        SAEnum(
            PolicyDocumentStatus,
            name="policy_document_status",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
        default=PolicyDocumentStatus.ACTIVE,
        server_default=PolicyDocumentStatus.ACTIVE.value,
        index=True,
    )
    ingestion_status = Column(
        SAEnum(
            PolicyIngestionStatus,
            name="policy_ingestion_status",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
        default=PolicyIngestionStatus.PENDING,
        server_default=PolicyIngestionStatus.PENDING.value,
        index=True,
    )
    effective_from = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    effective_until = Column(DateTime(timezone=True), nullable=True)
    content_length = Column(Integer, nullable=True)
    chunk_count = Column(Integer, nullable=False, default=0, server_default="0")
    metadata_info = Column(JSON, nullable=False, default=dict)
    retrieved_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    creator = relationship("User", foreign_keys=[created_by])
