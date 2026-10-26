"""Offline service tests: mocked actor sessions and audits, no database fixtures."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.core.security import Principal
from app.schemas.patients import PatientRegistrationRequest
from app.services import patients

NURSE = UUID("10000000-0000-4000-8000-000000000001")
PATIENT = UUID("20000000-0000-4000-8000-000000000001")
CONSENT = UUID("30000000-0000-4000-8000-000000000001")
REQUEST = UUID("40000000-0000-4000-8000-000000000001")
KEY = UUID("50000000-0000-4000-8000-000000000001")
PRINCIPAL = Principal(NURSE, "Nurse Fictional", "nurse", "fictional-session-hash")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
NOTICES = frozenset(
    {("care", "TEST-ONLY", "en"), ("uploads", "TEST-ONLY", "en"), ("future_ai", "TEST-ONLY", "en")}
)


def decision(granted=True):
    return dict(purpose="care", granted=granted, notice_version="TEST-ONLY", language="en")


def body():
    return dict(name="Adult Fictional", age=36, preferred_language="hi", consents=[decision()])


def consent_response(granted=True, revoked=False):
    return dict(
        id=CONSENT,
        purpose="care",
        granted=granted,
        notice_version="TEST-ONLY",
        language="en",
        created_at=NOW,
        revoked_at=NOW if revoked else None,
        status="revoked" if revoked else ("granted" if granted else "declined"),
    )


def registration_response():
    return dict(
        id=PATIENT,
        reference="FICTIONAL-001",
        preferred_language="hi",
        created_at=NOW,
        care_access="enabled",
        consents=[consent_response()],
    )


def directory_row(identity=PATIENT):
    return dict(
        id=identity,
        reference="FICTIONAL-001",
        name="Adult Fictional",
        age=36,
        age_recorded_on=NOW.date(),
        preferred_language="hi",
    )


def detail_row():
    return dict(
        **directory_row(),
        sex=None,
        contact_reference=None,
        history="Fictional history",
        allergies=None,
        current_medicines=None,
        created_at=NOW,
    )


class SessionStub:
    def __init__(self, state):
        self.state = state
        self.executed = []
        self.function_calls = []

    def execute(self, statement, parameters=None):
        self.executed.append((statement, parameters))
        if isinstance(statement, Select):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    all=lambda: self.state.rows, one_or_none=lambda: self.state.detail
                )
            )
        return None

    def scalar(self, statement, parameters=None):
        if isinstance(statement, Select):
            return self.state.nurse
        if "transaction_isolation" in str(statement):
            return self.state.isolation
        self.function_calls.append((str(statement), parameters))
        if self.state.failure is not None:
            raise self.state.failure
        return self.state.function_result


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(
        engine=object(),
        events=[],
        active=False,
        rows=[],
        detail=None,
        nurse=NURSE,
        isolation="read committed",
        failure=None,
        commit_failure=None,
        function_result=registration_response(),
        actors=[],
    )
    state.db = SessionStub(state)

    @contextmanager
    def actor(engine, identity):
        assert engine is state.engine
        state.actors.append(identity)
        state.events.append("begin")
        state.active = True
        try:
            yield state.db
        except BaseException:
            state.events.append("rollback")
            raise
        else:
            if state.commit_failure is not None:
                state.events.append("commit_failed")
                raise state.commit_failure
            state.events.append("commit")
        finally:
            state.active = False
            state.events.append("released")

    def access_audit(*args):
        assert not state.active, "Audit must not nest a clinical pool checkout"
        state.events.append("audit")

    state.audit = Mock(side_effect=access_audit)
    monkeypatch.setattr(patients, "actor_session", actor)
    monkeypatch.setattr(patients, "_audit_access", state.audit)
    state.service = patients.PatientService(state.engine, approved_consent_notices=NOTICES)
    return state


def test_registration_uses_server_principal_parameters_and_atomic_db_audit(harness):
    data = {**body(), "name": "Fictional'); SELECT private; --"}
    result = harness.service.register(PRINCIPAL, data, idempotency_key=KEY, request_id=REQUEST)
    assert result.id == PATIENT
    assert harness.actors == [NURSE]
    statement, parameters = harness.db.function_calls[0]
    assert statement == "SELECT app.register_patient(CAST(:payload AS jsonb), :key, :request)"
    assert data["name"] not in statement
    assert parameters["key"] == KEY and parameters["request"] == REQUEST
    submitted = json.loads(parameters["payload"])
    assert submitted["name"] == data["name"]
    assert submitted["preferred_language"] == "hi"
    assert submitted["consents"][0]["language"] == "en"
    assert [item["purpose"] for item in submitted["consents"]] == ["care"]
    assert "registered_by" not in submitted
    assert harness.events == ["begin", "commit", "released"]
    harness.audit.assert_not_called()


def test_timeouts_precede_sql_and_remain_transaction_local(harness):
    harness.service.register(PRINCIPAL, body(), idempotency_key=KEY, request_id=REQUEST)
    assert [str(query) for query, _ in harness.db.executed] == [
        "SET LOCAL statement_timeout = '10s'",
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL idle_in_transaction_session_timeout = '10s'",
    ]


@pytest.mark.parametrize("isolation", ["repeatable read", "serializable", None])
def test_unexpected_isolation_rolls_back_before_function_call(harness, isolation):
    harness.isolation = isolation
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(PRINCIPAL, body(), idempotency_key=KEY, request_id=REQUEST)
    assert error.value.status_code == 503
    assert not harness.db.function_calls
    assert "rollback" in harness.events


def test_default_empty_catalogue_denies_registration_and_updates_without_db(harness):
    service = patients.PatientService(harness.engine)
    for operation in (
        lambda: service.register(PRINCIPAL, body(), idempotency_key=KEY, request_id=REQUEST),
        lambda: service.record_consent(
            PRINCIPAL,
            PATIENT,
            {**decision(), "expected_current_id": CONSENT},
            idempotency_key=KEY,
            request_id=REQUEST,
        ),
    ):
        with pytest.raises(patients.PatientServiceError) as error:
            operation()
        assert error.value.status_code == 422
    assert not harness.events


@pytest.mark.parametrize(
    "catalogue",
    [
        None,
        [],
        frozenset({"not-a-tuple"}),
        frozenset({("care", "", "en")}),
        frozenset({("care", "TEST", "xx")}),
        frozenset({("care", "TEST\x00", "en")}),
    ],
)
def test_invalid_trusted_catalogue_is_rejected(harness, catalogue):
    with pytest.raises(ValueError, match="Invalid trusted consent notice configuration"):
        patients.PatientService(harness.engine, approved_consent_notices=catalogue)
    assert not harness.events


@pytest.mark.parametrize(
    "changes",
    [
        {"registered_by": str(NURSE)},
        {"id": str(PATIENT)},
        {"fictional": True},
        {"age": 17},
        {"consents": [decision(False)], "history": "Fictional private history"},
        {"consents": [{**decision(), "language": "hi"}]},
    ],
)
def test_invalid_body_never_opens_actor_session(harness, changes):
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(
            PRINCIPAL, {**body(), **changes}, idempotency_key=KEY, request_id=REQUEST
        )
    assert error.value.status_code == 422
    assert not harness.events


def test_mutated_prevalidated_model_is_revalidated_without_input_warnings(harness, recwarn):
    validated = PatientRegistrationRequest.model_validate(
        body(), context={"approved_consent_notices": NOTICES}
    )
    validated.age = "FICTIONAL_PRIVATE_MARKER"
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(PRINCIPAL, validated, idempotency_key=KEY, request_id=REQUEST)
    assert "FICTIONAL_PRIVATE_MARKER" not in str(error.value)
    assert not recwarn.list and not harness.events


@pytest.mark.parametrize(
    "principal",
    [
        {"id": NURSE, "role": "nurse"},
        NURSE,
        Principal(NURSE, "Doctor Fictional", "doctor", "fictional"),
        Principal(str(NURSE), "Nurse Fictional", "nurse", "fictional"),
    ],
)
def test_only_server_principal_with_nurse_role_and_uuid_is_accepted(harness, principal):
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(principal, body(), idempotency_key=KEY, request_id=REQUEST)
    assert error.value.status_code == 403 and not harness.events


@pytest.mark.parametrize("key", [None, str(KEY), UUID("50000000-0000-1000-8000-000000000001")])
def test_idempotency_key_must_be_explicit_uuid4(harness, key):
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(PRINCIPAL, body(), idempotency_key=key, request_id=REQUEST)
    assert error.value.status_code == 422 and not harness.events


def test_consent_update_separates_expected_id_from_decision_payload(harness):
    harness.function_result = dict(consent=consent_response(False), care_access="blocked")
    result = harness.service.record_consent(
        PRINCIPAL,
        PATIENT,
        {**decision(False), "expected_current_id": CONSENT},
        idempotency_key=KEY,
        request_id=REQUEST,
    )
    assert result.consent.granted is False
    statement, parameters = harness.db.function_calls[0]
    assert "app.record_consent_decision" in statement
    assert parameters["expected"] == CONSENT and parameters["patient"] == PATIENT
    assert json.loads(parameters["payload"]) == decision(False)
    harness.audit.assert_not_called()


def test_revocation_works_without_notice_catalogue(harness):
    harness.function_result = dict(consent=consent_response(revoked=True), care_access="blocked")
    service = patients.PatientService(harness.engine)
    result = service.revoke_consent(
        PRINCIPAL, PATIENT, CONSENT, {"purpose": "care"}, idempotency_key=KEY, request_id=REQUEST
    )
    assert result.care_access == "blocked"
    statement, parameters = harness.db.function_calls[0]
    assert statement == "SELECT app.revoke_consent(:patient, :consent, :purpose, :key, :request)"
    assert parameters == dict(
        patient=PATIENT, consent=CONSENT, purpose="care", key=KEY, request=REQUEST
    )
    harness.audit.assert_not_called()


def test_registration_status_uses_limited_response_and_database_audit(harness):
    result = harness.service.registration_status(PRINCIPAL, PATIENT, request_id=REQUEST)
    assert "name" not in result.model_dump() and "history" not in result.model_dump()
    assert harness.db.function_calls[0] == (
        "SELECT app.patient_registration_status(:patient, :request)",
        {"patient": PATIENT, "request": REQUEST},
    )
    harness.audit.assert_not_called()


@pytest.mark.parametrize(
    "method", ["register", "registration_status", "record_consent", "revoke_consent"]
)
def test_invalid_function_response_rolls_back_before_commit(harness, method):
    harness.function_result = {"unexpected": "FICTIONAL_PRIVATE_MARKER"}
    operations = {
        "register": lambda: harness.service.register(
            PRINCIPAL, body(), idempotency_key=KEY, request_id=REQUEST
        ),
        "registration_status": lambda: harness.service.registration_status(
            PRINCIPAL, PATIENT, request_id=REQUEST
        ),
        "record_consent": lambda: harness.service.record_consent(
            PRINCIPAL,
            PATIENT,
            {**decision(), "expected_current_id": CONSENT},
            idempotency_key=KEY,
            request_id=REQUEST,
        ),
        "revoke_consent": lambda: harness.service.revoke_consent(
            PRINCIPAL,
            PATIENT,
            CONSENT,
            {"purpose": "care"},
            idempotency_key=KEY,
            request_id=REQUEST,
        ),
    }
    with pytest.raises(patients.PatientServiceError) as error:
        operations[method]()
    assert error.value.status_code == 503
    assert "FICTIONAL_PRIVATE_MARKER" not in str(error.value)
    assert harness.events == ["begin", "rollback", "released"]


def database_error(primary, state="P0001"):
    original = Exception("FICTIONAL_PRIVATE_DRIVER_MARKER")
    original.sqlstate = state
    original.diag = SimpleNamespace(message_primary=primary)
    return DBAPIError("FICTIONAL_PRIVATE_SQL", {}, original, hide_parameters=True)


@pytest.mark.parametrize(
    "primary,state,expected",
    [
        ("registration.forbidden", "P0001", 403),
        ("registration.not_found", "P0001", 404),
        ("registration.invalid_input", "P0001", 422),
        ("registration.care_required", "P0001", 422),
        ("registration.notice_not_approved", "P0001", 422),
        ("registration.idempotency_conflict", "P0001", 409),
        ("registration.consent_conflict", "P0001", 409),
        ("registration.ambiguous_history", "P0001", 503),
        ("registration.history_immutable", "P0001", 503),
        ("registration.invalid_state", "P0001", 503),
        ("registration.operation_failed", "P0001", 503),
        ("registration.forbidden", "42601", 503),
        ("registration.forbidden EXTRA_PRIVATE_MARKER", "P0001", 503),
        ("private-value", "08006", 503),
        ("private-value", "57014", 503),
        ("private-value", "55P03", 503),
    ],
)
def test_database_errors_are_exactly_allowlisted_and_sanitized(harness, primary, state, expected):
    harness.failure = database_error(primary, state)
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(PRINCIPAL, body(), idempotency_key=KEY, request_id=REQUEST)
    assert error.value.status_code == expected
    assert "PRIVATE" not in str(error.value)
    assert error.value.__suppress_context__
    assert "rollback" in harness.events
    assert len(harness.db.function_calls) == 1


def test_commit_failure_is_not_reported_as_success_or_retried(harness):
    harness.commit_failure = SQLAlchemyError("FICTIONAL_PRIVATE_COMMIT_FAILURE")
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(PRINCIPAL, body(), idempotency_key=KEY, request_id=REQUEST)
    assert error.value.status_code == 503 and "PRIVATE" not in str(error.value)
    assert harness.events == ["begin", "commit_failed", "released"]
    assert len(harness.db.function_calls) == 1


def test_directory_projects_minimal_columns_and_uses_bound_keyset_pagination(harness):
    cursor = UUID("20000000-0000-4000-8000-000000000000")
    second = UUID("20000000-0000-4000-8000-000000000002")
    harness.rows = [directory_row(), directory_row(second)]
    result = harness.service.list_patients(PRINCIPAL, request_id=REQUEST, limit=1, cursor=cursor)
    assert len(result.items) == 1 and result.next_cursor == str(PATIENT)
    query = next(q for q, _ in harness.db.executed if isinstance(q, Select))
    assert set(query.selected_columns.keys()) == set(directory_row())
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert str(cursor) not in sql and str(NURSE) not in sql
    assert cursor in compiled.params.values() and NURSE in compiled.params.values()
    assert "fictional IS true" in sql and "BETWEEN" in sql and "EXISTS" in sql
    assert "app.nurses" in sql and "app.staff_profiles" in sql
    assert "ORDER BY app.patients.id" in sql
    assert 2 in compiled.params.values()
    assert harness.events == ["begin", "commit", "released", "audit"]
    harness.audit.assert_called_once_with(
        harness.engine, "patient.list", "success", REQUEST, NURSE, None
    )


def test_detail_is_projected_and_audited_before_return(harness):
    harness.detail = detail_row()
    result = harness.service.get_patient(PRINCIPAL, PATIENT, request_id=REQUEST)
    assert result.history == "Fictional history"
    query = next(q for q, _ in harness.db.executed if isinstance(q, Select))
    assert set(query.selected_columns.keys()) == set(detail_row())
    assert "registered_by" not in str(query) and "FOR UPDATE" not in str(query)
    harness.audit.assert_called_once_with(
        harness.engine, "patient.read", "success", REQUEST, NURSE, PATIENT
    )
    assert harness.events[-2:] == ["released", "audit"]


@pytest.mark.parametrize("operation", ["list", "detail", "not_found"])
def test_access_audit_failure_never_returns_protected_results(harness, operation):
    harness.audit.side_effect = RuntimeError("FICTIONAL_PRIVATE_AUDIT_FAILURE")
    harness.rows = [directory_row()]
    harness.detail = detail_row() if operation == "detail" else None
    with pytest.raises(patients.PatientServiceError) as error:
        if operation == "list":
            harness.service.list_patients(PRINCIPAL, request_id=REQUEST)
        else:
            harness.service.get_patient(PRINCIPAL, PATIENT, request_id=REQUEST)
    assert error.value.code == "audit_unavailable"
    assert "PRIVATE" not in str(error.value) and not harness.active


def test_missing_or_rls_hidden_patient_has_same_safe_not_found_and_denial_audit(harness):
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.get_patient(PRINCIPAL, PATIENT, request_id=REQUEST)
    assert error.value.status_code == 404 and str(PATIENT) not in str(error.value)
    harness.audit.assert_called_once_with(
        harness.engine, "patient.read", "denied", REQUEST, NURSE, None
    )


@pytest.mark.parametrize("method", ["list", "detail"])
def test_read_requires_current_nurse_profile(harness, method):
    harness.nurse = None
    with pytest.raises(patients.PatientServiceError) as error:
        if method == "list":
            harness.service.list_patients(PRINCIPAL, request_id=REQUEST)
        else:
            harness.service.get_patient(PRINCIPAL, PATIENT, request_id=REQUEST)
    assert error.value.status_code == 403 and "rollback" in harness.events
    assert not any(isinstance(q, Select) for q, _ in harness.db.executed)


@pytest.mark.parametrize("limit", [0, 101, True, "20", None])
def test_list_limit_is_bounded_before_database(harness, limit):
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.list_patients(PRINCIPAL, request_id=REQUEST, limit=limit)
    assert error.value.status_code == 422 and not harness.events


@pytest.mark.parametrize("method", ["list", "detail"])
def test_invalid_read_response_fails_before_commit_or_access_audit(harness, method):
    harness.rows = [{"id": PATIENT}]
    harness.detail = {"id": PATIENT}
    with pytest.raises(patients.PatientServiceError) as error:
        if method == "list":
            harness.service.list_patients(PRINCIPAL, request_id=REQUEST)
        else:
            harness.service.get_patient(PRINCIPAL, PATIENT, request_id=REQUEST)
    assert error.value.status_code == 503 and "rollback" in harness.events
    harness.audit.assert_not_called()


@pytest.mark.parametrize("failure", [None, "insert", "commit"])
def test_separate_read_audit_transaction_is_bounded_and_atomic(monkeypatch, failure):
    events = []
    db = Mock()
    engine = object()
    record = []

    @contextmanager
    def begin():
        events.append("begin")
        try:
            yield
            if failure == "commit":
                raise SQLAlchemyError("FICTIONAL_PRIVATE_COMMIT_FAILURE")
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    @contextmanager
    def session(actual_engine):
        assert actual_engine is engine
        try:
            yield db
        finally:
            events.append("released")

    def add(row):
        record.append(row)
        if failure == "insert":
            raise SQLAlchemyError("FICTIONAL_PRIVATE_INSERT_FAILURE")

    db.begin.side_effect = begin
    db.add.side_effect = add
    monkeypatch.setattr(patients, "Session", session)
    if failure:
        with pytest.raises(SQLAlchemyError):
            patients._audit_access(engine, "patient.read", "success", REQUEST, NURSE, PATIENT)
        assert events == ["begin", "rollback", "released"]
    else:
        patients._audit_access(engine, "patient.read", "success", REQUEST, NURSE, PATIENT)
        assert events == ["begin", "commit", "released"]
    assert [str(call.args[0]) for call in db.execute.call_args_list] == [
        "SET LOCAL statement_timeout = '10s'",
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL idle_in_transaction_session_timeout = '10s'",
    ]
    assert len(record) == 1
    row = record[0]
    assert (row.action, row.outcome, row.request_id, row.actor_id, row.resource_id) == (
        "patient.read",
        "success",
        REQUEST,
        NURSE,
        PATIENT,
    )


def test_empty_directory_is_still_audited(harness):
    response = harness.service.list_patients(PRINCIPAL, request_id=REQUEST)
    assert response.items == [] and response.next_cursor is None
    harness.audit.assert_called_once()


@pytest.mark.parametrize("method", ["detail", "status", "cursor", "request"])
def test_invalid_identifiers_fail_before_database_and_are_not_echoed(harness, method):
    marker = "FICTIONAL_PRIVATE_IDENTIFIER"
    operations = {
        "detail": lambda: harness.service.get_patient(PRINCIPAL, marker, request_id=REQUEST),
        "status": lambda: harness.service.registration_status(
            PRINCIPAL, marker, request_id=REQUEST
        ),
        "cursor": lambda: harness.service.list_patients(
            PRINCIPAL, cursor=marker, request_id=REQUEST
        ),
        "request": lambda: harness.service.register(
            PRINCIPAL, body(), idempotency_key=KEY, request_id=marker
        ),
    }
    with pytest.raises(patients.PatientServiceError) as error:
        operations[method]()
    assert error.value.status_code == 422 and marker not in str(error.value)
    assert not harness.events


def test_connection_checkout_failure_is_sanitized_without_retry(harness, monkeypatch):
    actor = Mock(side_effect=SQLAlchemyError("FICTIONAL_PRIVATE_CONNECTION_FAILURE"))
    monkeypatch.setattr(patients, "actor_session", actor)
    with pytest.raises(patients.PatientServiceError) as error:
        harness.service.register(PRINCIPAL, body(), idempotency_key=KEY, request_id=REQUEST)
    assert error.value.status_code == 503 and "PRIVATE" not in str(error.value)
    actor.assert_called_once_with(harness.engine, NURSE)
