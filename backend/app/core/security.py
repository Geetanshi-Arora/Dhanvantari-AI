import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import actor_session
from app.models.foundation import AuthSession, StaffProfile
from app.services.audit import audit

SESSION_COOKIE = "dhanvantari_session"
CSRF_COOKIE = "dhanvantari_csrf"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def require_origin(request: Request):
    if request.headers.get("origin") != request.app.state.settings.allowed_origin:
        raise HTTPException(403, "Untrusted request origin")


@dataclass(frozen=True)
class Principal:
    id: UUID
    display_name: str
    role: str
    session_hash: str


def current_staff(request: Request) -> Principal:
    state = request.app.state
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw or len(raw) > 256:
        raise HTTPException(401, "Authentication required")
    with Session(state.engine) as db:
        session = db.get(AuthSession, digest(raw))
        if not session or session.expires_at <= datetime.now(UTC):
            raise HTTPException(401, "Session expired")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_origin(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not csrf or not secrets.compare_digest(digest(csrf), session.csrf_hash):
                raise HTTPException(403, "Invalid CSRF token")
        try:
            access_token = state.cipher.decrypt(session.encrypted_access_token.encode()).decode()
        except InvalidToken:
            raise HTTPException(401, "Session expired") from None
        expected_identity, session_hash = session.staff_id, session.token_hash
    # Release the connection before network I/O and avoid nesting database checkouts.
    identity = state.auth.user(access_token)
    if identity != expected_identity:
        raise HTTPException(401, "Session identity mismatch")
    with actor_session(state.engine, identity) as actor_db:
        staff = actor_db.scalar(select(StaffProfile).where(StaffProfile.id == identity))
        principal = (
            Principal(staff.id, staff.display_name, staff.role, session_hash)
            if staff and staff.active
            else None
        )
    if principal is None:
        audit(state.engine, "auth.access", "denied", request.state.request_id, identity)
        raise HTTPException(403, "Active clinician account required")
    return principal


def require_role(*roles):
    from fastapi import Depends

    def dependency(request: Request, principal: Principal = Depends(current_staff)):
        if principal.role not in roles:
            audit(
                request.app.state.engine,
                "auth.role",
                "denied",
                request.state.request_id,
                principal.id,
            )
            raise HTTPException(403, "Role not permitted")
        return principal

    return dependency
