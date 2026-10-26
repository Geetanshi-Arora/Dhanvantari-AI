"""Private registration metadata: never directly exposed to the runtime role."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.foundation import Base


class ConsentNoticeVersion(Base):
    __tablename__ = "consent_notice_versions"
    __table_args__ = (
        CheckConstraint("purpose IN ('care','uploads','future_ai')"),
        CheckConstraint("length(notice_version) BETWEEN 1 AND 40"),
        CheckConstraint("language IN ('en','hi','bn','ta','te','mr','gu','pa')"),
        {"schema": "app"},
    )
    purpose: Mapped[str] = mapped_column(String(20), primary_key=True)
    notice_version: Mapped[str] = mapped_column(String(40), primary_key=True)
    language: Mapped[str] = mapped_column(String(16), primary_key=True)
    accepts_new: Mapped[bool] = mapped_column(Boolean, server_default="false")


class PatientOperationKey(Base):
    __tablename__ = "patient_operation_keys"
    __table_args__ = (
        CheckConstraint("operation IN ('registration','consent','revocation')"),
        CheckConstraint("octet_length(request_fingerprint) = 32"),
        CheckConstraint("result IS NULL OR jsonb_typeof(result) = 'object'"),
        {"schema": "app"},
    )
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("app.nurses.id"), primary_key=True)
    operation: Mapped[str] = mapped_column(String(20), primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
