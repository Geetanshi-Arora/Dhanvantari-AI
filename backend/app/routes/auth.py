import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.core.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Principal,
    current_staff,
    digest,
    require_origin,
)
from app.db.session import actor_session
from app.models.foundation import AuditLog, AuthSession, StaffProfile
from app.schemas.auth import LoginRequest, StaffResponse
from app.services.audit import audit

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


def throttle(request, email):
    settings = request.app.state.settings
    # Separate IP and account buckets prevent account guessing across workers/IPs.
    # Ignore forwarded headers: only a trusted proxy may supply a verified client address.
    keys = [
        digest("ip:" + (request.client.host if request.client else "unknown")),
        digest("email:" + email.lower()),
    ]
    blocked = False
    with Session(request.app.state.engine) as db, db.begin():
        for key in sorted(keys):
            count = db.execute(
                text("""
                INSERT INTO app.login_throttles AS t (key, count, expires_at)
                VALUES (:key, 1, now() + :window * interval '1 second')
                ON CONFLICT (key) DO UPDATE SET
                  count = CASE WHEN t.expires_at <= now() THEN 1 ELSE t.count + 1 END,
                  expires_at = CASE WHEN t.expires_at <= now()
                    THEN now() + :window * interval '1 second' ELSE t.expires_at END
                RETURNING count
            """),
                {"key": key, "window": settings.login_window_seconds},
            ).scalar_one()
            blocked |= count > settings.login_limit
    if blocked:
        audit(request.app.state.engine, "auth.login", "throttled", request.state.request_id)
        raise HTTPException(
            429, "Please retry later", headers={"Retry-After": str(settings.login_window_seconds)}
        )


@router.post("/login", response_model=StaffResponse, dependencies=[Depends(require_origin)])
def login(body: LoginRequest, request: Request, response: Response):
    state = request.app.state
    throttle(request, str(body.email))
    try:
        token, provider_lifetime = state.auth.login(
            str(body.email), body.password.get_secret_value()
        )
        identity = state.auth.user(token)
        with actor_session(state.engine, identity) as db:
            staff = db.scalar(select(StaffProfile).where(StaffProfile.id == identity))
            if not staff or not staff.active:
                raise HTTPException(403, "Active clinician account required")
            result = StaffResponse.model_validate(staff)
    except HTTPException as exc:
        audit(
            state.engine,
            "auth.login",
            "unavailable" if exc.status_code == 503 else "denied",
            request.state.request_id,
        )
        raise
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    ttl = min(state.settings.session_ttl_seconds, provider_lifetime)
    with Session(state.engine) as db, db.begin():
        previous = request.cookies.get(SESSION_COOKIE)
        if previous and len(previous) <= 256:
            db.execute(delete(AuthSession).where(AuthSession.token_hash == digest(previous)))
        db.add(
            AuthSession(
                token_hash=digest(raw),
                staff_id=identity,
                encrypted_access_token=state.cipher.encrypt(token.encode()).decode(),
                csrf_hash=digest(csrf),
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            )
        )
        db.add(
            AuditLog(
                id=uuid4(),
                actor_id=identity,
                action="auth.login",
                outcome="success",
                request_id=request.state.request_id,
            )
        )
    for name, value, httponly in [(SESSION_COOKIE, raw, True), (CSRF_COOKIE, csrf, False)]:
        response.set_cookie(
            name,
            value,
            httponly=httponly,
            secure=state.settings.cookie_secure,
            samesite="strict",
            max_age=ttl,
            path="/api",
        )
    return result


@router.get("/me", response_model=StaffResponse)
def me(principal: Principal = Depends(current_staff)):
    return StaffResponse(id=principal.id, display_name=principal.display_name, role=principal.role)


@router.get("/csrf")
def csrf(request: Request, principal: Principal = Depends(current_staff)):
    # Same-origin JS retrieves the token; no permissive CORS configuration.
    value = request.cookies.get(CSRF_COOKIE, "")
    if not value:
        raise HTTPException(403, "CSRF cookie missing; sign in again")
    return {"csrf_token": value}


@router.post("/logout", status_code=204, dependencies=[Depends(require_origin)])
def logout(request: Request, response: Response):
    # Local revocation must remain possible after expiry, deactivation or provider outage.
    raw = request.cookies.get(SESSION_COOKIE, "")
    with Session(request.app.state.engine) as db, db.begin():
        session = db.get(AuthSession, digest(raw)) if raw and len(raw) <= 256 else None
        if session:
            csrf = request.headers.get("x-csrf-token", "")
            if not csrf or not secrets.compare_digest(digest(csrf), session.csrf_hash):
                raise HTTPException(403, "Invalid CSRF token")
            db.delete(session)
            db.add(
                AuditLog(
                    id=uuid4(),
                    actor_id=session.staff_id,
                    action="auth.logout",
                    outcome="success",
                    request_id=request.state.request_id,
                )
            )
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name,
            path="/api",
            secure=request.app.state.settings.cookie_secure,
            httponly=name == SESSION_COOKIE,
            samesite="strict",
        )
