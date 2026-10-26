"""Prepared API→service→PostgreSQL tests; execution needs separate explicit opt-in.

No production configuration, provider network, credentials or notice approvals are
loaded. Every fixture depends on the guarded fresh-cluster local_pg fixture.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_patient_database import (
    DOCTOR,
    INACTIVE,
    NOTICE,
    NURSE,
    OTHER,
    counts,
    decision,
    payload,
    staff,
)

from app.core.config import Settings
from app.main import create_app
from app.schemas.patients import (
    ConsentMutationResponse,
    PatientDetailResponse,
    PatientDirectoryResponse,
    PatientRegistrationResponse,
    RegistrationStatusResponse,
)
from app.services.patients import PatientService

ORIGIN = "http://localhost:8000"
ROOT = "/api/v1/patients"
APPROVALS = frozenset((purpose, NOTICE, "en") for purpose in ("care", "uploads", "future_ai"))
ACCOUNTS = {
    "nurse@example.com": NURSE,
    "other@example.com": OTHER,
    "doctor@example.com": DOCTOR,
    "inactive@example.com": INACTIVE,
}


class FictionalAuth:
    """Provider boundary only; real cookie sessions, CSRF and staff DB checks remain."""

    def login(self, email, password):
        if email not in ACCOUNTS or password != "fictional-test-only":
            raise HTTPException(401, "Invalid or expired credentials")
        return email, 3600

    def user(self, token):
        if token not in ACCOUNTS:
            raise HTTPException(401, "Invalid or expired credentials")
        return ACCOUNTS[token]


@pytest.fixture
def api(local_pg):
    # local_pg checks opt-in/environment and installs all guards BEFORE this code.
    local_pg.migrate("0004")
    local_pg.create_runtime()
    admin = local_pg.engine()
    runtime = local_pg.engine(local_pg.url("dhanvantari_runtime"))
    staff(admin)
    with admin.begin() as db:
        for purpose in ("care", "uploads", "future_ai"):
            db.execute(
                text(
                    "INSERT INTO app.consent_notice_versions "
                    "(purpose,notice_version,language,accepts_new) VALUES(:p,:v,'en',true)"
                ),
                {"p": purpose, "v": NOTICE},
            )
    settings = Settings(
        _env_file=None,
        environment="development",
        runtime_credential_file=None,
        runtime_pooler_host=None,
        runtime_ca_file=None,
        session_ttl_seconds=1800,
        login_limit=10,
        login_window_seconds=900,
        database_url=local_pg.url("dhanvantari_runtime").render_as_string(hide_password=False),
        supabase_url="https://fictional.supabase.co",
        supabase_publishable_key="fictional-publishable-key",
        session_encryption_key=Fernet.generate_key().decode(),
        allowed_origin=ORIGIN,
        cookie_secure=False,
    )
    app = create_app(settings, engine=runtime, auth=FictionalAuth())
    app.state.patient_service = PatientService(runtime, approved_consent_notices=APPROVALS)
    with ExitStack() as clients:

        def client(account="nurse", *, login=True):
            http = clients.enter_context(TestClient(app, base_url=ORIGIN))
            if login:
                response = http.post(
                    "/api/v1/auth/login",
                    headers={"Origin": ORIGIN},
                    json={"email": account + "@example.com", "password": "fictional-test-only"},
                )
                assert response.status_code == 200
            return http

        yield app, admin, runtime, client
    # ExitStack closes every client/lifespan before local_pg disposes/stops/removes.


def write_headers(client, key=None):
    csrf = client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf.json()["csrf_token"],
        "Idempotency-Key": str(key if key is not None else uuid4()),
    }


def register(client, data=None, key=None):
    return client.post(
        ROOT, json=payload() if data is None else data, headers=write_headers(client, key)
    )


def consent_path(patient):
    return f"{ROOT}/{patient}/consents"


def change(client, patient, expected, *, granted=True, purpose="care", key=None):
    expected = str(expected) if isinstance(expected, UUID) else expected
    return client.post(
        consent_path(patient),
        headers=write_headers(client, key),
        json={**decision(purpose, granted), "expected_current_id": expected},
    )


def revoke(client, patient, consent, *, purpose="care", key=None):
    return client.post(
        f"{consent_path(patient)}/{consent}/revoke",
        headers=write_headers(client, key),
        json={"purpose": purpose},
    )


def registered(client, care=True):
    response = register(client, payload(care))
    assert response.status_code == 201
    return PatientRegistrationResponse.model_validate(response.json())


def ledger(admin):
    with admin.connect() as db:
        return db.execute(
            text(
                "SELECT action,outcome,resource_id FROM app.audit_logs "
                "WHERE action LIKE 'patient.%' OR action LIKE 'consent.%' ORDER BY action"
            )
        ).all()


def fail_audits(admin, action):
    # Disposable-only failure injection, after login. Never alter the real audit code.
    with admin.begin() as db:
        db.execute(
            text(
                "CREATE FUNCTION app.test_api_audit_failure() RETURNS trigger LANGUAGE plpgsql "
                "SET search_path=pg_catalog AS $$ BEGIN "
                "IF NEW.action=TG_ARGV[0] THEN RAISE EXCEPTION 'FICTIONAL_PRIVATE_MARKER'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        # Only these fixed literals are accepted; DDL trigger arguments cannot bind.
        assert action in {"patient.register", "patient.list", "patient.read"}
        db.execute(
            text(
                "CREATE TRIGGER test_api_audit_failure BEFORE INSERT ON app.audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION app.test_api_audit_failure('" + action + "')"
            )
        )


def test_six_endpoints_real_contracts_and_atomic_audits(api):
    _, admin, _, clients = api
    http = clients()
    patient = registered(http)
    identity = str(patient.id)
    listing = http.get(ROOT)
    assert listing.status_code == 200
    assert PatientDirectoryResponse.model_validate(listing.json()).items[0].id == patient.id
    detail = http.get(f"{ROOT}/{identity}")
    assert detail.status_code == 200
    assert PatientDetailResponse.model_validate(detail.json()).name == "Adult Fictional"
    status = http.get(f"{ROOT}/{identity}/registration")
    assert status.status_code == 200
    assert RegistrationStatusResponse.model_validate(status.json()).care_access == "enabled"
    update = change(http, identity, None, purpose="uploads")
    assert update.status_code == 201
    consent = ConsentMutationResponse.model_validate(update.json()).consent
    revoked = revoke(http, identity, consent.id, purpose="uploads")
    assert revoked.status_code == 200
    assert ConsentMutationResponse.model_validate(revoked.json()).consent.status == "revoked"
    actions = {row.action for row in ledger(admin)}
    assert {
        "patient.register",
        "patient.list",
        "patient.read",
        "patient.registration_status",
        "consent.record.care",
        "consent.record.uploads",
        "consent.revoke.uploads",
    } <= actions
    with admin.connect() as db:
        assert db.scalar(text("SELECT registered_by FROM app.patients")) == NURSE
        assert db.execute(text("SELECT staff_id FROM app.patient_assignments")).scalars().all() == [
            NURSE
        ]
        assert db.scalar(text("SELECT count(*) FROM app.consents WHERE purpose='future_ai'")) == 0


@pytest.mark.parametrize("endpoint", ["register", "list", "detail", "status", "change", "revoke"])
def test_doctor_rejected_on_every_endpoint(api, endpoint):
    _, admin, _, clients = api
    nurse, doctor = clients(), clients("doctor")
    patient = registered(nurse)
    before = counts(admin)[:4]
    responses = {
        "register": lambda: register(doctor),
        "list": lambda: doctor.get(ROOT),
        "detail": lambda: doctor.get(f"{ROOT}/{patient.id}"),
        "status": lambda: doctor.get(f"{ROOT}/{patient.id}/registration"),
        "change": lambda: change(doctor, patient.id, patient.consents[0].id),
        "revoke": lambda: revoke(doctor, patient.id, patient.consents[0].id),
    }
    assert responses[endpoint]().status_code == 403
    assert counts(admin)[:4] == before


def test_unassigned_nurse_sees_no_directory_detail_status_or_write_access(api):
    _, admin, _, clients = api
    nurse, other = clients(), clients("other")
    patient = registered(nurse)
    assert other.get(ROOT).json()["items"] == []
    for path in (f"{ROOT}/{patient.id}", f"{ROOT}/{patient.id}/registration"):
        assert other.get(path).status_code == 404
    before = counts(admin)[:4]
    assert change(other, patient.id, patient.consents[0].id).status_code == 404
    assert revoke(other, patient.id, patient.consents[0].id).status_code == 404
    assert counts(admin)[:4] == before


@pytest.mark.parametrize("care_state", ["declined", "revoked"])
def test_blocked_care_hides_clinical_reads_preserves_limited_status(api, care_state):
    _, _, _, clients = api
    http = clients()
    patient = registered(http, care=care_state != "declined")
    if care_state == "revoked":
        assert revoke(http, patient.id, patient.consents[0].id).status_code == 200
    assert http.get(ROOT).json()["items"] == []
    assert http.get(f"{ROOT}/{patient.id}").status_code == 404
    response = http.get(f"{ROOT}/{patient.id}/registration")
    assert response.status_code == 200
    result = RegistrationStatusResponse.model_validate(response.json())
    assert result.care_access == "blocked" and result.consents[0].status == care_state
    assert not {"name", "history", "allergies", "current_medicines"} & response.json().keys()


def test_idempotent_registration_and_conflicting_retry(api):
    _, admin, _, clients = api
    http, key = clients(), uuid4()
    first = register(http, key=key)
    assert first.status_code == 201
    before = counts(admin)
    retry = register(http, key=key)
    assert retry.status_code == 201 and retry.json()["id"] == first.json()["id"]
    assert counts(admin) == before
    conflict = register(http, {**payload(), "age": 37}, key)
    assert conflict.status_code == 409
    assert counts(admin) == before


def test_concurrent_registration_same_key_has_one_patient(api):
    _, admin, _, clients = api
    http = [clients(), clients()]
    supplied = [write_headers(client) for client in http]
    key, gate = uuid4(), Barrier(2)

    def attempt(index):
        gate.wait(timeout=10)
        return http[index].post(
            ROOT, json=payload(), headers={**supplied[index], "Idempotency-Key": str(key)}
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(attempt, index) for index in range(2)]
        responses = [result.result(timeout=30) for result in results]
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert counts(admin)[:4] == (1, 1, 1, 1)
    assert [row.action for row in ledger(admin)].count("patient.register") == 1


def test_concurrent_consent_changes_one_winner_preserves_history(api):
    _, admin, _, clients = api
    http = [clients(), clients()]
    patient = registered(http[0])
    supplied, gate = [write_headers(client) for client in http], Barrier(2)

    def attempt(index):
        gate.wait(timeout=10)
        return http[index].post(
            consent_path(patient.id),
            headers=supplied[index],
            json={
                **decision(granted=bool(index)),
                "expected_current_id": str(patient.consents[0].id),
            },
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(attempt, index) for index in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    assert sorted(response.status_code for response in responses) == [201, 409]
    with admin.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM app.consents")) == 2
        assert db.scalar(text("SELECT count(*) FROM app.consents WHERE revoked_at IS NULL")) == 1
        assert db.scalar(
            text("SELECT revoked_at IS NOT NULL AND granted FROM app.consents WHERE id=:id"),
            {"id": patient.consents[0].id},
        )


def test_consent_retry_revocation_retry_and_regrant_keep_history(api):
    _, admin, _, clients = api
    http = clients()
    patient = registered(http)
    key = uuid4()
    response = change(http, patient.id, patient.consents[0].id, key=key)
    assert response.status_code == 201
    current = response.json()["consent"]["id"]
    before = counts(admin)
    assert (
        change(http, patient.id, patient.consents[0].id, key=key).json()["consent"]["id"] == current
    )
    assert counts(admin) == before
    revocation_key = uuid4()
    assert revoke(http, patient.id, current, key=revocation_key).status_code == 200
    before = counts(admin)
    assert revoke(http, patient.id, current, key=revocation_key).status_code == 200
    assert counts(admin) == before
    assert change(http, patient.id, current).status_code == 201
    with admin.connect() as db:
        rows = db.execute(
            text("SELECT granted,revoked_at FROM app.consents ORDER BY created_at")
        ).all()
        assert len(rows) == 3 and all(row.granted for row in rows)
        assert [row.revoked_at is None for row in rows] == [False, False, True]


@pytest.mark.parametrize("operation", ["list", "detail"])
def test_actual_access_audit_insert_failure_returns_no_clinical_data(api, operation):
    _, admin, _, clients = api
    http = clients()
    patient = registered(http)
    before = counts(admin)
    fail_audits(admin, "patient.list" if operation == "list" else "patient.read")
    response = http.get(ROOT if operation == "list" else f"{ROOT}/{patient.id}")
    assert response.status_code == 503
    assert response.json() == {"detail": "Patient access audit unavailable"}
    assert (
        "Adult Fictional" not in response.text and "FICTIONAL_PRIVATE_MARKER" not in response.text
    )
    assert counts(admin) == before


def test_registration_audit_database_error_rolls_back_all_writes(api):
    _, admin, _, clients = api
    http = clients()
    before = counts(admin)
    fail_audits(admin, "patient.register")
    response = register(http)
    assert response.status_code == 503 and "FICTIONAL_PRIVATE_MARKER" not in response.text
    assert response.json() == {"detail": "Patient operation unavailable"}
    assert counts(admin) == before


@pytest.mark.parametrize("catalogue", ["server_empty", "database_empty", "unapproved_translation"])
def test_notice_checks_fail_closed_without_writes(api, catalogue):
    app, admin, runtime, clients = api
    http = clients()
    data = payload()
    if catalogue == "server_empty":
        app.state.patient_service = PatientService(runtime)
    elif catalogue == "database_empty":
        with admin.begin() as db:
            db.execute(text("DELETE FROM app.consent_notice_versions"))
    else:
        data["consents"][0]["language"] = "hi"
    before = counts(admin)
    response = register(http, data)
    assert response.status_code == 422
    assert counts(admin) == before


@pytest.mark.parametrize("failure", ["origin", "csrf", "missing_session"])
def test_auth_guards_prevent_registration(api, failure):
    _, admin, _, clients = api
    http = clients()
    supplied = write_headers(http)
    if failure == "missing_session":
        http.cookies.clear()
    else:
        supplied["Origin" if failure == "origin" else "X-CSRF-Token"] = "fictional-invalid"
    before = counts(admin)[:4]
    response = http.post(ROOT, json=payload(), headers=supplied)
    assert response.status_code == (401 if failure == "missing_session" else 403)
    assert counts(admin)[:4] == before


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_body_never_reaches_database_writes(api, chunked):
    _, admin, _, clients = api
    http = clients()
    supplied, before = write_headers(http), counts(admin)
    content = (b"x" * 4096 for _ in range(9)) if chunked else b"x" * 32769
    assert http.post(ROOT, content=content, headers=supplied).status_code == 413
    assert counts(admin) == before


def test_response_contract_failure_rolls_back_real_function_writes(api, monkeypatch):
    from pydantic import ValidationError

    from app.services import patients

    _, admin, _, clients = api
    http = clients()
    before = counts(admin)

    class InvalidResponse:
        @staticmethod
        def model_validate(value):
            assert value["id"]  # Real DB function returned, but COMMIT has not happened.
            raise ValidationError.from_exception_data("FictionalResponse", [])

    monkeypatch.setattr(patients, "PatientRegistrationResponse", InvalidResponse)
    response = register(http)
    assert response.status_code == 503
    assert counts(admin) == before


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["fictional-invalid"],
        ["40000000-0000-4000-8000-000000000001", "40000000-0000-4000-8000-000000000001"],
    ],
)
def test_missing_malformed_duplicate_idempotency_headers_cannot_write(api, values):
    _, admin, _, clients = api
    http = clients()
    supplied = [
        (name, value) for name, value in write_headers(http).items() if name != "Idempotency-Key"
    ]
    supplied.extend(("Idempotency-Key", value) for value in values)
    before = counts(admin)
    assert http.post(ROOT, json=payload(), headers=supplied).status_code == 422
    assert counts(admin) == before


def test_inactive_nurse_cannot_sign_in_or_register(api):
    _, admin, _, clients = api
    http = clients(login=False)
    before = counts(admin)[:4]
    response = http.post(
        "/api/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"email": "inactive@example.com", "password": "fictional-test-only"},
    )
    assert response.status_code == 403
    assert http.get(ROOT).status_code == 401
    assert counts(admin)[:4] == before


def test_care_decline_with_clinical_text_rejected_without_writes(api):
    _, admin, _, clients = api
    http = clients()
    before = counts(admin)
    response = register(http, {**payload(False), "history": "FICTIONAL_PRIVATE_MARKER"})
    assert response.status_code == 422 and "FICTIONAL_PRIVATE_MARKER" not in response.text
    assert counts(admin) == before
