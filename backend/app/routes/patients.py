"""Nurse-only patient HTTP contracts; clinical authorization remains in the service/DB."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.security import Principal, require_role
from app.schemas.patients import (
    ConsentMutationResponse,
    PatientDetailResponse,
    PatientDirectoryResponse,
    PatientRegistrationResponse,
    RegistrationStatusResponse,
)
from app.services.patients import PatientServiceError

router = APIRouter(prefix="/api/v1/patients", tags=["patients"])
nurse = require_role("nurse")


def idempotency_key(request: Request) -> UUID:
    values = request.headers.getlist("idempotency-key")
    try:
        if len(values) != 1:
            raise ValueError
        value = UUID(values[0])
        if value.version != 4 or str(value) != values[0].lower():
            raise ValueError
        return value
    except ValueError:
        raise HTTPException(422, "One UUIDv4 Idempotency-Key header is required") from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(value):
    raise ValueError


async def write_body(request: Request, principal: Principal = Depends(nurse)):
    # Authentication, origin and CSRF checks finish before application JSON parsing.
    # PatientBodyLimit bounds the actual ASGI stream, including chunked requests.
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
        or request.headers.get("content-encoding", "identity").lower() != "identity"
    ):
        raise HTTPException(415, "An unencoded application/json body is required")
    try:
        value = json.loads(
            await request.body(), object_pairs_hook=_object, parse_constant=_constant
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid patient request") from None


def invoke(request: Request, operation: str, principal: Principal, *args, **kwargs):
    try:
        return getattr(request.app.state.patient_service, operation)(
            principal, *args, request_id=request.state.request_id, **kwargs
        )
    except PatientServiceError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("", status_code=201, response_model=PatientRegistrationResponse)
def register(
    request: Request,
    principal: Principal = Depends(nurse),
    key: UUID = Depends(idempotency_key),
    body: dict = Depends(write_body),
):
    return invoke(request, "register", principal, body, idempotency_key=key)


@router.get("", response_model=PatientDirectoryResponse)
def directory(
    request: Request,
    principal: Principal = Depends(nurse),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: UUID | None = None,
):
    return invoke(request, "list_patients", principal, limit=limit, cursor=cursor)


@router.get("/{patient_id}", response_model=PatientDetailResponse)
def detail(patient_id: UUID, request: Request, principal: Principal = Depends(nurse)):
    return invoke(request, "get_patient", principal, patient_id)


@router.get("/{patient_id}/registration", response_model=RegistrationStatusResponse)
def registration_status(patient_id: UUID, request: Request, principal: Principal = Depends(nurse)):
    return invoke(request, "registration_status", principal, patient_id)


@router.post("/{patient_id}/consents", status_code=201, response_model=ConsentMutationResponse)
def record_consent(
    patient_id: UUID,
    request: Request,
    principal: Principal = Depends(nurse),
    key: UUID = Depends(idempotency_key),
    body: dict = Depends(write_body),
):
    return invoke(request, "record_consent", principal, patient_id, body, idempotency_key=key)


@router.post("/{patient_id}/consents/{consent_id}/revoke", response_model=ConsentMutationResponse)
def revoke_consent(
    patient_id: UUID,
    consent_id: UUID,
    request: Request,
    principal: Principal = Depends(nurse),
    key: UUID = Depends(idempotency_key),
    body: dict = Depends(write_body),
):
    return invoke(
        request, "revoke_consent", principal, patient_id, consent_id, body, idempotency_key=key
    )
