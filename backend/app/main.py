from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import Settings, load_runtime_settings
from app.core.http import AuthBodyLimit, PatientBodyLimit
from app.core.runtime_credentials import runtime_database_url
from app.db.session import check_runtime_role, make_engine
from app.routes.auth import router
from app.routes.patients import router as patient_router
from app.services.audit import audit
from app.services.patients import PatientService
from app.services.supabase_auth import SupabaseAuth


def create_app(settings: Settings | None = None, *, engine=None, auth=None):
    try:
        return _create_app(settings, engine=engine, auth=auth)
    except Exception:
        raise RuntimeError("Application configuration failed; check private settings") from None


def _create_app(settings: Settings | None = None, *, engine=None, auth=None):
    settings = settings or load_runtime_settings()
    db_engine = engine if engine is not None else make_engine(runtime_database_url(settings))
    client = httpx.Client(timeout=10, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(app):
        try:
            try:
                check_runtime_role(db_engine)
            except Exception:
                raise RuntimeError("Database startup validation failed") from None
            yield
        finally:
            client.close()
            if engine is None:
                db_engine.dispose()

    app = FastAPI(
        title="Dhanvantari AI", version="0.2.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.add_middleware(AuthBodyLimit)
    app.add_middleware(PatientBodyLimit)
    app.state.settings = settings
    app.state.engine = db_engine
    app.state.patient_service = PatientService(db_engine)
    app.state.cipher = Fernet(settings.session_encryption_key.get_secret_value().encode())
    app.state.auth = auth if auth is not None else SupabaseAuth(settings, client)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.allowed_origin).hostname]
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        request.state.request_id = uuid4()
        if (
            request.headers.get("content-length", "").isdigit()
            and int(request.headers["content-length"]) > 16384
            and request.url.path.startswith("/api/v1/auth")
        ):
            response = JSONResponse({"detail": "Request too large"}, status_code=413)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(HTTPException)
    async def rejected(request, exc):
        if exc.status_code in {401, 403}:
            reason = (
                "origin"
                if exc.detail == "Untrusted request origin"
                else "csrf"
                if exc.detail == "Invalid CSRF token"
                else "session"
                if exc.status_code == 401
                else "forbidden"
            )
            # Fixed categories only: no cookies, origins, URLs, account names or provider text.
            try:
                audit(db_engine, "security.denied", reason, request.state.request_id)
            except SQLAlchemyError:
                return JSONResponse({"detail": "Security audit unavailable"}, status_code=503)
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # FastAPI's default details can echo submitted passwords; never return input values.
        return JSONResponse(
            {"detail": [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]},
            status_code=422,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, exc):
        return JSONResponse({"detail": "Database operation unavailable"}, status_code=503)

    @app.get("/api/v1/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    def ready():
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.include_router(router)
    app.include_router(patient_router)
    app.mount(
        "/",
        StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend", html=True),
        name="frontend",
    )
    return app
