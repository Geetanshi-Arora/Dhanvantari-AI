"""Persistent authentication and audit models; no application passwords are stored."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StaffProfile(Base):
    __tablename__ = "staff_profiles"
    __table_args__ = (CheckConstraint("role IN ('nurse','doctor')"), {"schema": "app"})
    id: Mapped[UUID] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(10))
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Nurse(Base):
    __tablename__ = "nurses"
    __table_args__ = {"schema": "app"}
    id: Mapped[UUID] = mapped_column(ForeignKey("app.staff_profiles.id"), primary_key=True)
    registration_number: Mapped[str] = mapped_column(String(100), unique=True)


class Doctor(Base):
    __tablename__ = "doctors"
    __table_args__ = {"schema": "app"}
    id: Mapped[UUID] = mapped_column(ForeignKey("app.staff_profiles.id"), primary_key=True)
    registration_number: Mapped[str] = mapped_column(String(100), unique=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = {"schema": "app"}
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    staff_id: Mapped[UUID] = mapped_column(ForeignKey("app.staff_profiles.id"), index=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    __table_args__ = {"schema": "app"}
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditLog(Base):
    __mapper_args__ = {"eager_defaults": False}
    __tablename__ = "audit_logs"
    __table_args__ = {"schema": "app"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("app.staff_profiles.id"))
    action: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[UUID | None]
    request_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
