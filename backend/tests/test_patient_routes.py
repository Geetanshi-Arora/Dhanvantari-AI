"""Offline HTTP integration: real auth dependencies, mocked persistence and provider."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core import security
from app.core.config import Settings
from app.main import create_app
from app.routes import auth as auth_routes
from app.services.patients import PatientService, PatientServiceError

NURSE = UUID("10000000-0000-4000-8000-000000000001")
PATIENT = UUID("20000000-0000-4000-8000-000000000001")
CONSENT = UUID("30000000-0000-4000-8000-000000000001")
KEY = "40000000-0000-4000-8000-000000000001"
ORIGIN = "http://localhost:8000"
NOTICE = frozenset({("care", "TEST-ONLY", "en")})
NOW = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
ROOT = "/api/v1/patients"


def decision():
    return dict(purpose="care", granted=True, notice_version="TEST-ONLY", language="en")


def registration():
    return dict(name="Adult Fictional", age=36, preferred_language="en", consents=[decision()])


def consent_response():
    return dict(**decision(), id=str(CONSENT), status="granted", created_at=NOW, revoked_at=None)


def status():
    return dict(
        id=str(PATIENT),
        reference="FICTIONAL-001",
        preferred_language="en",
        created_at=NOW,
        care_access="enabled",
        consents=[consent_response()],
    )


def directory_item():
    return dict(
        id=str(PATIENT),
        reference="FICTIONAL-001",
        name="Adult Fictional",
        age=36,
        age_recorded_on="2026-01-01",
        preferred_language="en",
    )


@pytest.fixture
def web(monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused/unused",
        supabase_url="https://fictional.supabase.co",
        supabase_publishable_key="fictional-publishable-key",
        session_encryption_key=Fernet.generate_key().decode(),
        allowed_origin=ORIGIN,
        cookie_secure=False,
    )
    engine = object()
    provider = Mock()
    provider.user.return_value = NURSE
    app = create_app(settings, engine=engine, auth=provider)
    service = Mock()
    service.register.return_value = status()
    service.registration_status.return_value = status()
    service.list_patients.return_value = dict(items=[directory_item()], next_cursor=None)
    service.get_patient.return_value = dict(
        **directory_item(),
        sex=None,
        contact_reference=None,
        history="Fictional history",
        allergies=None,
        current_medicines=None,
        created_at=NOW,
    )
    service.record_consent.return_value = dict(consent=consent_response(), care_access="enabled")
    service.revoke_consent.return_value = dict(
        consent={**consent_response(), "status": "revoked", "revoked_at": NOW},
        care_access="blocked",
    )
    app.state.patient_service = service
    session = SimpleNamespace(
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        staff_id=NURSE,
        token_hash=security.digest("fictional-session"),
        csrf_hash=security.digest("fictional-csrf"),
        encrypted_access_token=app.state.cipher.encrypt(b"fictional-provider-token").decode(),
    )
    staff = SimpleNamespace(id=NURSE, role="nurse", active=True, display_name="Nurse Fictional")
    db = Mock()
    db.get.return_value = session
    db.scalar.return_value = staff
    events = []

    @contextmanager
    def ordinary_session(_engine):
        yield db

    @contextmanager
    def actor_session(_engine, identity):
        events.append("actor")
        assert provider.user.called  # Verified Auth identity precedes clinical identity.
        assert identity == NURSE
        yield db

    audit = Mock()
    monkeypatch.setattr(security, "Session", ordinary_session)
    monkeypatch.setattr(security, "actor_session", actor_session)
    monkeypatch.setattr(security, "audit", audit)
    monkeypatch.setattr("app.main.audit", audit)
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set("dhanvantari_session", "fictional-session", path="/api")
    client.cookies.set("dhanvantari_csrf", "fictional-csrf", path="/api")
    try:
        yield SimpleNamespace(
            client=client,
            app=app,
            service=service,
            db=db,
            session=session,
            staff=staff,
            provider=provider,
            audit=audit,
            events=events,
            engine=engine,
        )
    finally:
        client.close()


def headers():
    return {"Origin": ORIGIN, "X-CSRF-Token": "fictional-csrf", "Idempotency-Key": KEY}


CASES = [
    ("POST", ROOT, "register", registration(), 201),
    ("GET", ROOT, "list_patients", None, 200),
    ("GET", f"{ROOT}/{PATIENT}", "get_patient", None, 200),
    ("GET", f"{ROOT}/{PATIENT}/registration", "registration_status", None, 200),
    (
        "POST",
        f"{ROOT}/{PATIENT}/consents",
        "record_consent",
        {**decision(), "expected_current_id": None},
        201,
    ),
    (
        "POST",
        f"{ROOT}/{PATIENT}/consents/{CONSENT}/revoke",
        "revoke_consent",
        {"purpose": "care"},
        200,
    ),
]


@pytest.mark.parametrize("method,path,operation,body,code", CASES)
def test_six_contracts_and_verified_principal(web, method, path, operation, body, code):
    response = web.client.request(method, path, json=body, headers=headers())
    assert response.status_code == code, response.text
    call = getattr(web.service, operation).call_args
    assert call.args[0].id == NURSE and call.args[0].role == "nurse"
    assert isinstance(call.kwargs["request_id"], UUID)
    if method == "POST":
        assert call.kwargs["idempotency_key"] == UUID(KEY)
        assert call.args[-1] == body
    if operation in {"get_patient", "registration_status", "record_consent", "revoke_consent"}:
        assert call.args[1] == PATIENT
    if operation == "revoke_consent":
        assert call.args[2] == CONSENT
    assert response.headers["cache-control"] == "no-store"
    assert web.events == ["actor"]


@pytest.mark.parametrize("method,path,operation,body,code", CASES)
def test_doctors_cannot_use_any_endpoint(web, method, path, operation, body, code):
    web.staff.role = "doctor"
    assert web.client.request(method, path, json=body, headers=headers()).status_code == 403
    assert not web.service.mock_calls


@pytest.mark.parametrize("mode", ["missing", "unknown", "expired", "corrupt", "mismatch"])
def test_invalid_sessions(web, mode):
    if mode == "missing":
        web.client.cookies.clear()
    elif mode == "unknown":
        web.db.get.return_value = None
    elif mode == "expired":
        web.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif mode == "corrupt":
        web.session.encrypted_access_token = "fictional-corruption"
    else:
        web.provider.user.return_value = PATIENT
    assert web.client.get(ROOT).status_code == 401
    assert not web.service.mock_calls and not web.events


@pytest.mark.parametrize("kind", ["origin", "csrf", "missing_origin", "missing_csrf"])
@pytest.mark.parametrize(
    "method,path,operation,body,code", [case for case in CASES if case[0] == "POST"]
)
def test_existing_origin_csrf_guards(web, kind, method, path, operation, body, code):
    supplied = headers()
    key = "Origin" if "origin" in kind else "X-CSRF-Token"
    if kind.startswith("missing"):
        supplied.pop(key)
    else:
        supplied[key] = "fictional-invalid"
    response = web.client.post(path, headers=supplied, json=body)
    assert response.status_code == 403
    assert not web.service.mock_calls and not web.provider.user.called


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["bad"],
        ["10000000-0000-1000-8000-000000000001"],
        [KEY, KEY],
        [KEY + "," + KEY],
        [" " + KEY],
    ],
)
@pytest.mark.parametrize(
    "method,path,operation,body,code", [case for case in CASES if case[0] == "POST"]
)
def test_idempotency_headers(web, values, method, path, operation, body, code):
    supplied = [(k, v) for k, v in headers().items() if k != "Idempotency-Key"]
    supplied += [("Idempotency-Key", value) for value in values]
    assert web.client.post(path, headers=supplied, json=body).status_code == 422
    assert not web.service.mock_calls


@pytest.mark.parametrize(
    "raw",
    [
        b'{"history":"PRIVATE_MARKER",',
        b"[]",
        b"null",
        b'{"name":"PRIVATE_MARKER","name":"duplicate"}',
        b'{"age":NaN}',
        b"\xff",
    ],
)
def test_invalid_json_is_sanitized(web, raw):
    response = web.client.post(
        ROOT, headers={**headers(), "Content-Type": "application/json"}, content=raw
    )
    assert response.status_code == 422 and "PRIVATE_MARKER" not in response.text
    assert not web.service.mock_calls


@pytest.mark.parametrize(
    "content_type,encoding", [("text/plain", "identity"), ("application/json", "gzip")]
)
def test_content_type_and_encoding(web, content_type, encoding):
    response = web.client.post(
        ROOT,
        content=b"{}",
        headers={**headers(), "Content-Type": content_type, "Content-Encoding": encoding},
    )
    assert response.status_code == 415 and not web.service.mock_calls


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_actual_stream_rejected_before_auth(web, chunked):
    content = (b"x" * 4096 for _ in range(9)) if chunked else b"x" * 32769
    response = web.client.post(ROOT, headers=headers(), content=content)
    assert response.status_code == 413
    assert not web.provider.user.called and not web.service.mock_calls


@pytest.mark.parametrize("catalogue,language", [(frozenset(), "en"), (NOTICE, "hi")])
@pytest.mark.parametrize("update", [False, True])
def test_real_service_notice_checks_fail_closed(web, catalogue, language, update):
    web.app.state.patient_service = PatientService(web.engine, approved_consent_notices=catalogue)
    data = {**decision(), "language": language}
    payload = (
        {**data, "expected_current_id": None} if update else {**registration(), "consents": [data]}
    )
    path = f"{ROOT}/{PATIENT}/consents" if update else ROOT
    response = web.client.post(path, json=payload, headers=headers())
    assert response.status_code == 422


def test_trusted_context_and_actual_service_response_validation(web, monkeypatch):
    from app.services import patients

    db = Mock()
    db.scalar.side_effect = ["read committed", status()]

    @contextmanager
    def actor(engine, identity):
        assert engine is web.engine and identity == NURSE
        yield db

    monkeypatch.setattr(patients, "actor_session", actor)
    web.app.state.patient_service = PatientService(web.engine, approved_consent_notices=NOTICE)
    response = web.client.post(ROOT, json=registration(), headers=headers())
    assert response.status_code == 201
    assert "TEST-ONLY" in response.text


@pytest.mark.parametrize(
    "category,code",
    [
        ("not_found", 404),
        ("forbidden", 403),
        ("invalid_input", 422),
        ("idempotency_conflict", 409),
        ("consent_conflict", 409),
        ("notice_not_approved", 422),
        ("unavailable", 503),
        ("audit_unavailable", 503),
    ],
)
def test_safe_service_error_mapping(web, category, code):
    web.service.get_patient.side_effect = PatientServiceError(category)
    response = web.client.get(f"{ROOT}/{PATIENT}")
    assert response.status_code == code and "history" not in response.text
    assert "Fictional" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        f"{ROOT}/PRIVATE_MARKER",
        f"{ROOT}?cursor=PRIVATE_MARKER",
        f"{ROOT}?limit=PRIVATE_MARKER",
        f"{ROOT}?limit=101",
    ],
)
def test_path_and_query_validation_is_sanitized(web, path):
    response = web.client.get(path)
    assert response.status_code == 422 and "PRIVATE_MARKER" not in response.text
    assert not web.service.mock_calls


def test_auth_me_csrf_and_missing_session_regression(web):
    assert web.client.get("/api/v1/auth/me").json()["role"] == "nurse"
    assert web.client.get("/api/v1/auth/csrf").json() == {"csrf_token": "fictional-csrf"}
    web.client.cookies.clear()
    assert web.client.get("/api/v1/auth/me").status_code == 401


def test_auth_login_validation_origin_and_body_limit_unchanged(web):
    assert web.client.post("/api/v1/auth/login", json={}).status_code == 403
    response = web.client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": "fictional@example.com", "password": {"secret": "PRIVATE_MARKER"}},
    )
    assert response.status_code == 422 and "PRIVATE_MARKER" not in response.text
    assert web.client.post("/api/v1/auth/login", content=b"x" * 16385).status_code == 413
    chunks = (b"x" * 4096 for _ in range(5))
    assert web.client.post("/api/v1/auth/login", content=chunks).status_code == 413


def test_provider_failure_prevents_patient_access(web):
    web.provider.user.side_effect = HTTPException(503, "Authentication unavailable")
    assert web.client.get(ROOT).status_code == 503
    assert not web.events and not web.service.mock_calls


def test_deactivation_blocks_access(web):
    web.staff.active = False
    assert web.client.get(ROOT).status_code == 403
    assert not web.service.mock_calls


def test_auth_login_regression_with_mocked_persistence(web, monkeypatch):
    from contextlib import nullcontext

    @contextmanager
    def session(engine):
        assert engine is web.engine
        yield web.db

    @contextmanager
    def actor(engine, identity):
        assert engine is web.engine and identity == NURSE
        yield web.db

    web.db.begin.side_effect = lambda: nullcontext()
    web.provider.login.return_value = ("fictional-provider-token", 3600)
    monkeypatch.setattr(auth_routes, "Session", session)
    monkeypatch.setattr(auth_routes, "actor_session", actor)
    monkeypatch.setattr(auth_routes, "throttle", Mock())
    response = web.client.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": "fictional@example.com", "password": "fictional-test-password"},
    )
    assert response.status_code == 200 and response.json()["role"] == "nurse"
    cookies = response.headers.get_list("set-cookie")
    assert "HttpOnly" in cookies[0] and "SameSite=strict" in cookies[0]
    assert "fictional-provider-token" not in response.text


def test_auth_logout_regression_with_mocked_persistence(web, monkeypatch):
    from contextlib import nullcontext

    @contextmanager
    def session(engine):
        assert engine is web.engine
        yield web.db

    web.db.begin.side_effect = lambda: nullcontext()
    monkeypatch.setattr(auth_routes, "Session", session)
    assert web.client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
    response = web.client.post("/api/v1/auth/logout", headers=headers())
    assert response.status_code == 204
    web.db.delete.assert_called_once_with(web.session)
    assert all("Max-Age=0" in cookie for cookie in response.headers.get_list("set-cookie"))


@pytest.mark.parametrize(
    "changes",
    [
        {"registered_by": str(NURSE)},
        {"age": 17},
        {"consents": [{**decision(), "granted": False}], "history": "PRIVATE_MARKER"},
        {"history": "PRIVATE_MARKER" * 1000},
    ],
)
def test_real_request_validation_never_echoes_clinical_input(web, changes):
    web.app.state.patient_service = PatientService(web.engine, approved_consent_notices=NOTICE)
    response = web.client.post(ROOT, json={**registration(), **changes}, headers=headers())
    assert response.status_code == 422 and "PRIVATE_MARKER" not in response.text


@pytest.mark.parametrize("operation", ["list", "detail"])
def test_actual_service_audit_failure_blocks_http_result(web, monkeypatch, operation):
    from app.services import patients

    db = Mock()
    db.scalar.side_effect = ["read committed", NURSE]
    rows = db.execute.return_value.mappings.return_value
    rows.all.return_value = [directory_item()]
    rows.one_or_none.return_value = web.service.get_patient.return_value

    @contextmanager
    def actor(engine, identity):
        assert engine is web.engine and identity == NURSE
        yield db

    monkeypatch.setattr(patients, "actor_session", actor)
    monkeypatch.setattr(patients, "_audit_access", Mock(side_effect=RuntimeError("PRIVATE_MARKER")))
    web.app.state.patient_service = PatientService(web.engine)
    path = ROOT if operation == "list" else f"{ROOT}/{PATIENT}"
    response = web.client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Patient access audit unavailable"}
    assert "Fictional" not in response.text and "PRIVATE_MARKER" not in response.text


@pytest.mark.parametrize("size,expected", [(32768, 204), (32769, 413)])
def test_actual_asgi_multichunk_body_bound(size, expected):
    import asyncio

    from app.core.http import PatientBodyLimit

    forwarded = []
    sent = []
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 16000, "more_body": True},
            {"type": "http.request", "body": b"x" * (size - 16000), "more_body": False},
        ]
    )

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        forwarded.append(await receive())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(
        PatientBodyLimit(downstream)(
            {"type": "http", "path": ROOT, "method": "POST", "headers": []}, receive, send
        )
    )
    assert sent[0]["status"] == expected
    if expected == 204:
        assert forwarded[0]["body"] == b"x" * size
    else:
        assert not forwarded


def test_replayed_registration_keeps_201_contract(web):
    for _ in range(2):
        assert web.client.post(ROOT, json=registration(), headers=headers()).status_code == 201
    assert web.service.register.call_count == 2
    assert all(
        call.kwargs["idempotency_key"] == UUID(KEY) for call in web.service.register.call_args_list
    )
