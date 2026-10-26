"""Patient operations using the restricted runtime engine; no HTTP routes.

Principal must be produced by current_staff/require_role, never deserialized from
client data. This layer does not independently verify JWTs. Database functions
recheck nurse authorization, and ordinary SELECTs retain clinical-role RLS.
Notice metadata is trusted server configuration, empty until explicitly approved;
retain historical approved versions for retries, with DB accepts_new authoritative
for new decisions. No administrator connection or table-write fallback is used.
"""

from contextlib import contextmanager
from typing import get_args
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import exists, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import Principal
from app.db.session import actor_session
from app.models.clinical import Patient
from app.models.foundation import AuditLog, Nurse, StaffProfile
from app.schemas.patients import (
    ConsentMutationResponse,
    ConsentPurpose,
    ConsentRevocationRequest,
    ConsentUpdateRequest,
    Language,
    PatientDetailResponse,
    PatientDirectoryItem,
    PatientDirectoryResponse,
    PatientRegistrationRequest,
    PatientRegistrationResponse,
    RegistrationStatusResponse,
)

_ERRORS = {
    "forbidden": (403, "Active nurse authorization required"),
    "not_found": (404, "Patient resource unavailable"),
    "invalid_input": (422, "Invalid patient request"),
    "care_required": (422, "Clinical information requires care consent"),
    "notice_not_approved": (422, "Consent notice or translation unavailable"),
    "idempotency_conflict": (409, "Idempotency key conflicts with the request"),
    "consent_conflict": (409, "Consent decision has changed"),
    "unavailable": (503, "Patient operation unavailable"),
    "audit_unavailable": (503, "Patient access audit unavailable"),
}
_DATABASE_ERRORS = {
    "registration." + code: code
    for code in (
        "forbidden",
        "not_found",
        "invalid_input",
        "care_required",
        "notice_not_approved",
        "idempotency_conflict",
        "consent_conflict",
    )
}
_DIRECTORY_COLUMNS = (
    Patient.id,
    Patient.reference,
    Patient.name,
    Patient.age,
    Patient.age_recorded_on,
    Patient.preferred_language,
)
_DETAIL_COLUMNS = (
    *_DIRECTORY_COLUMNS,
    Patient.sex,
    Patient.contact_reference,
    Patient.history,
    Patient.allergies,
    Patient.current_medicines,
    Patient.created_at,
)
_TIMEOUTS = (
    "SET LOCAL statement_timeout = '10s'",
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL idle_in_transaction_session_timeout = '10s'",
)


class PatientServiceError(Exception):
    """Allowlisted public category and message, without SQL or submitted values."""

    def __init__(self, code):
        self.code = code if code in _ERRORS else "unavailable"
        self.status_code, self.detail = _ERRORS[self.code]
        super().__init__(self.detail)


def _database_error(error):
    original = getattr(error, "orig", None)
    if getattr(original, "sqlstate", None) == "P0001":
        primary = getattr(getattr(original, "diag", None), "message_primary", None)
        code = _DATABASE_ERRORS.get(primary)
        if code:
            return PatientServiceError(code)
    return PatientServiceError("unavailable")


def _uuid(value, *, version=None):
    if not isinstance(value, UUID) or (version is not None and value.version != version):
        raise PatientServiceError("invalid_input")
    return value


def _audit_access(engine, action, outcome, request_id, actor_id, resource_id):
    # Same fixed-field convention as services.audit.audit, but bound this separate
    # runtime-role transaction too. The clinical session is already released.
    with Session(engine) as db, db.begin():
        for statement in _TIMEOUTS:
            db.execute(text(statement))
        db.add(
            AuditLog(
                id=uuid4(),
                action=action,
                outcome=outcome,
                request_id=request_id,
                actor_id=actor_id,
                resource_id=resource_id,
            )
        )


class PatientService:
    def __init__(self, engine, *, approved_consent_notices=frozenset()):
        if not isinstance(approved_consent_notices, frozenset) or any(
            not isinstance(entry, tuple)
            or len(entry) != 3
            or not all(isinstance(part, str) for part in entry)
            or entry[0] not in get_args(ConsentPurpose)
            or not 1 <= len(entry[1]) <= 40
            or "\x00" in entry[1]
            or entry[2] not in get_args(Language)
            for entry in approved_consent_notices
        ):
            raise ValueError("Invalid trusted consent notice configuration")
        self.engine = engine
        self._notices = approved_consent_notices

    def _principal(self, principal, request_id):
        if (
            not isinstance(principal, Principal)
            or principal.role != "nurse"
            or not isinstance(principal.id, UUID)
        ):
            raise PatientServiceError("forbidden")
        _uuid(request_id)

    def _request(self, model, body):
        try:
            # Revalidate field values even for mutable/model_construct() instances.
            data = (
                body.model_dump(mode="python", warnings=False)
                if isinstance(body, BaseModel)
                else body
            )
            return model.model_validate(data, context={"approved_consent_notices": self._notices})
        except ValidationError:
            raise PatientServiceError("invalid_input") from None

    @contextmanager
    def _transaction(self, principal):
        try:
            with actor_session(self.engine, principal.id) as db:
                for statement in _TIMEOUTS:
                    db.execute(text(statement))
                # actor_session already issued a SELECT; do not try changing the
                # isolation level here. Fail closed if server defaults differ.
                if (
                    db.scalar(text("SELECT current_setting('transaction_isolation')"))
                    != "read committed"
                ):
                    raise PatientServiceError("unavailable")
                yield db
        except SQLAlchemyError as error:
            raise _database_error(error) from None
        except ValidationError:
            # Response contract failure must roll back function writes/audits.
            raise PatientServiceError("unavailable") from None

    def _function(self, principal, request_id, statement, parameters, response_model):
        self._principal(principal, request_id)
        with self._transaction(principal) as db:
            result = db.scalar(text(statement), parameters)
            response = response_model.model_validate(result)
        # actor_session must finish COMMIT successfully before data is returned.
        return response

    def register(self, principal, body, *, idempotency_key, request_id):
        self._principal(principal, request_id)
        _uuid(idempotency_key, version=4)
        body = self._request(PatientRegistrationRequest, body)
        return self._function(
            principal,
            request_id,
            "SELECT app.register_patient(CAST(:payload AS jsonb), :key, :request)",
            {"payload": body.model_dump_json(), "key": idempotency_key, "request": request_id},
            PatientRegistrationResponse,
        )

    def registration_status(self, principal, patient_id, *, request_id):
        self._principal(principal, request_id)
        return self._function(
            principal,
            request_id,
            "SELECT app.patient_registration_status(:patient, :request)",
            {"patient": _uuid(patient_id), "request": request_id},
            RegistrationStatusResponse,
        )

    def record_consent(self, principal, patient_id, body, *, idempotency_key, request_id):
        self._principal(principal, request_id)
        _uuid(patient_id)
        _uuid(idempotency_key, version=4)
        body = self._request(ConsentUpdateRequest, body)
        return self._function(
            principal,
            request_id,
            "SELECT app.record_consent_decision(:patient, CAST(:payload AS jsonb), "
            ":expected, :key, :request)",
            {
                "patient": patient_id,
                "payload": body.model_dump_json(exclude={"expected_current_id"}),
                "expected": body.expected_current_id,
                "key": idempotency_key,
                "request": request_id,
            },
            ConsentMutationResponse,
        )

    def revoke_consent(
        self, principal, patient_id, consent_id, body, *, idempotency_key, request_id
    ):
        self._principal(principal, request_id)
        _uuid(patient_id)
        _uuid(consent_id)
        _uuid(idempotency_key, version=4)
        body = self._request(ConsentRevocationRequest, body)
        return self._function(
            principal,
            request_id,
            "SELECT app.revoke_consent(:patient, :consent, :purpose, :key, :request)",
            {
                "patient": patient_id,
                "consent": consent_id,
                "purpose": body.purpose,
                "key": idempotency_key,
                "request": request_id,
            },
            ConsentMutationResponse,
        )

    def _nurse_query(self, principal):
        return (
            select(Nurse.id)
            .join(StaffProfile, StaffProfile.id == Nurse.id)
            .where(
                Nurse.id == principal.id,
                StaffProfile.active.is_(True),
                StaffProfile.role == "nurse",
            )
        )

    def _read_query(self, principal, columns):
        # Do not disable row_security or replace RLS with application filtering.
        return select(*columns).where(
            Patient.fictional.is_(True),
            Patient.age.between(18, 120),
            exists(self._nurse_query(principal)),
        )

    def _access_audit(self, principal, request_id, action, outcome, resource_id=None):
        try:
            # Separate transaction after the clinical checkout is released; no
            # nested pool checkout and no clinical values in the audit event.
            _audit_access(self.engine, action, outcome, request_id, principal.id, resource_id)
        except Exception:
            raise PatientServiceError("audit_unavailable") from None

    def list_patients(self, principal, *, request_id, limit=20, cursor=None):
        self._principal(principal, request_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise PatientServiceError("invalid_input")
        if cursor is not None:
            _uuid(cursor)
        with self._transaction(principal) as db:
            if db.scalar(self._nurse_query(principal)) is None:
                raise PatientServiceError("forbidden")
            query = self._read_query(principal, _DIRECTORY_COLUMNS)
            if cursor is not None:
                query = query.where(Patient.id > cursor)
            rows = db.execute(query.order_by(Patient.id).limit(limit + 1)).mappings().all()
            items = [PatientDirectoryItem.model_validate(row) for row in rows[:limit]]
            response = PatientDirectoryResponse.model_validate(
                {
                    "items": items,
                    "next_cursor": str(items[-1].id) if len(rows) > limit else None,
                }
            )
        self._access_audit(principal, request_id, "patient.list", "success")
        return response

    def get_patient(self, principal, patient_id, *, request_id):
        self._principal(principal, request_id)
        _uuid(patient_id)
        found = None
        with self._transaction(principal) as db:
            if db.scalar(self._nurse_query(principal)) is None:
                raise PatientServiceError("forbidden")
            row = (
                db.execute(
                    self._read_query(principal, _DETAIL_COLUMNS).where(Patient.id == patient_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                found = PatientDetailResponse.model_validate(row)
        if found is None:
            self._access_audit(principal, request_id, "patient.read", "denied")
            raise PatientServiceError("not_found")
        self._access_audit(principal, request_id, "patient.read", "success", patient_id)
        return found
