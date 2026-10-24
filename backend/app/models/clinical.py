"""Phase 2 schema foundation. Clinical API writes are deliberately not granted yet."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.foundation import Base


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Patient(Record, Base):
    __tablename__ = "patients"
    __table_args__ = (CheckConstraint("age BETWEEN 0 AND 120"), {"schema": "app"})
    reference: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    age: Mapped[int]
    age_recorded_on: Mapped[date] = mapped_column(Date)
    sex: Mapped[str | None] = mapped_column(String(30))
    preferred_language: Mapped[str] = mapped_column(String(16))
    contact_reference: Mapped[str | None] = mapped_column(String(60))
    history: Mapped[str | None] = mapped_column(Text)
    allergies: Mapped[str | None] = mapped_column(Text)
    current_medicines: Mapped[str | None] = mapped_column(Text)
    registered_by: Mapped[UUID] = mapped_column(ForeignKey("app.nurses.id"))
    fictional: Mapped[bool] = mapped_column(Boolean, server_default="true")


class PatientAssignment(Base):
    __tablename__ = "patient_assignments"
    __table_args__ = {"schema": "app"}
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("app.patients.id"), primary_key=True)
    staff_id: Mapped[UUID] = mapped_column(ForeignKey("app.staff_profiles.id"), primary_key=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Consent(Record, Base):
    __tablename__ = "consents"
    __table_args__ = (
        CheckConstraint("purpose IN ('care','uploads','future_ai')"),
        Index(
            "uq_active_consent",
            "patient_id",
            "purpose",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {"schema": "app"},
    )
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("app.patients.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(20))
    notice_version: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(16))
    granted: Mapped[bool]
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app.nurses.id"))
    representative: Mapped[str | None] = mapped_column(String(80))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Visit(Record, Base):
    __tablename__ = "visits"
    __table_args__ = (
        CheckConstraint("status IN ('draft','pending','approved','rejected','referred')"),
        {"schema": "app"},
    )
    patient_id: Mapped[UUID] = mapped_column(ForeignKey("app.patients.id"), index=True)
    nurse_id: Mapped[UUID] = mapped_column(ForeignKey("app.nurses.id"))
    doctor_id: Mapped[UUID | None] = mapped_column(ForeignKey("app.doctors.id"), index=True)
    visit_date: Mapped[date] = mapped_column(Date)
    language: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class Symptom(Record, Base):
    __tablename__ = "symptoms"
    __table_args__ = {"schema": "app"}
    visit_id: Mapped[UUID] = mapped_column(ForeignKey("app.visits.id"), index=True)
    original_text: Mapped[str] = mapped_column(Text)
    original_language: Mapped[str] = mapped_column(String(16))
    translated_text: Mapped[str | None] = mapped_column(Text)
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app.nurses.id"))


class VitalSigns(Record, Base):
    __tablename__ = "vital_signs"
    __table_args__ = (
        CheckConstraint("temperature_c BETWEEN 25 AND 45"),
        CheckConstraint("pulse_bpm BETWEEN 1 AND 300"),
        CheckConstraint("systolic_mmhg BETWEEN 1 AND 350"),
        CheckConstraint("diastolic_mmhg BETWEEN 1 AND 250"),
        CheckConstraint("spo2_percent BETWEEN 0 AND 100"),
        CheckConstraint("respiratory_rate BETWEEN 1 AND 100"),
        CheckConstraint("weight_kg > 0 AND weight_kg <= 700"),
        CheckConstraint(
            "num_nonnulls(temperature_c,pulse_bpm,systolic_mmhg,"
            "diastolic_mmhg,spo2_percent,respiratory_rate,weight_kg) > 0"
        ),
        {"schema": "app"},
    )
    visit_id: Mapped[UUID] = mapped_column(ForeignKey("app.visits.id"), index=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app.nurses.id"))
    temperature_c: Mapped[float | None] = mapped_column(Numeric(4, 1))
    pulse_bpm: Mapped[int | None]
    systolic_mmhg: Mapped[int | None]
    diastolic_mmhg: Mapped[int | None]
    spo2_percent: Mapped[float | None] = mapped_column(Numeric(5, 2))
    respiratory_rate: Mapped[int | None]
    weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 2))


class UploadedFile(Record, Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (
        CheckConstraint("size_bytes > 0 AND size_bytes <= 10485760"),
        CheckConstraint("status IN ('quarantined','clean','rejected')"),
        {"schema": "app"},
    )
    visit_id: Mapped[UUID] = mapped_column(ForeignKey("app.visits.id"), index=True)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    mime_type: Mapped[str] = mapped_column(String(80))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("app.staff_profiles.id"))
    status: Mapped[str] = mapped_column(String(16), server_default="quarantined")


class MedicalReport(Record, Base):
    __tablename__ = "medical_reports"
    __table_args__ = {"schema": "app"}
    file_id: Mapped[UUID] = mapped_column(ForeignKey("app.uploaded_files.id"), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))


class UploadedImage(Record, Base):
    __tablename__ = "uploaded_images"
    __table_args__ = {"schema": "app"}
    file_id: Mapped[UUID] = mapped_column(ForeignKey("app.uploaded_files.id"), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))


class AIAssessment(Record, Base):
    __tablename__ = "ai_assessments"
    __table_args__ = {"schema": "app"}
    visit_id: Mapped[UUID] = mapped_column(ForeignKey("app.visits.id"), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str] = mapped_column(String(80))
    suggestions: Mapped[dict] = mapped_column(JSON)
    # There is intentionally no approval flag: only a separate doctor decision can authorize care.


class ClinicianDecision(Record, Base):
    __tablename__ = "clinician_decisions"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected','referred')"),
        UniqueConstraint("visit_id", "version"),
        {"schema": "app"},
    )
    visit_id: Mapped[UUID] = mapped_column(ForeignKey("app.visits.id"), index=True)
    doctor_id: Mapped[UUID] = mapped_column(ForeignKey("app.doctors.id"))
    assessment_id: Mapped[UUID | None] = mapped_column(ForeignKey("app.ai_assessments.id"))
    rationale: Mapped[str] = mapped_column(Text)
    treatment_instructions: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)


class Prescription(Record, Base):
    __tablename__ = "prescriptions"
    __table_args__ = {"schema": "app"}
    decision_id: Mapped[UUID] = mapped_column(ForeignKey("app.clinician_decisions.id"), index=True)
    doctor_id: Mapped[UUID] = mapped_column(ForeignKey("app.doctors.id"))


class PrescriptionItem(Record, Base):
    __tablename__ = "prescription_items"
    __table_args__ = {"schema": "app"}
    prescription_id: Mapped[UUID] = mapped_column(ForeignKey("app.prescriptions.id"), index=True)
    medication: Mapped[str] = mapped_column(String(200))
    dose: Mapped[str] = mapped_column(String(100))
    frequency: Mapped[str] = mapped_column(String(100))
    duration: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str | None] = mapped_column(Text)


class UrgentEscalation(Record, Base):
    __tablename__ = "urgent_escalations"
    __table_args__ = (
        CheckConstraint("status IN ('open','acknowledged','resolved')"),
        CheckConstraint(
            "(status = 'open' AND acknowledged_by IS NULL AND acknowledged_at IS NULL) "
            "OR (status <> 'open' AND acknowledged_by IS NOT NULL "
            "AND acknowledged_at IS NOT NULL)"
        ),
        {"schema": "app"},
    )
    visit_id: Mapped[UUID] = mapped_column(ForeignKey("app.visits.id"), index=True)
    flagged_by: Mapped[UUID] = mapped_column(ForeignKey("app.nurses.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default="open", index=True)
    acknowledged_by: Mapped[UUID | None] = mapped_column(ForeignKey("app.doctors.id"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
