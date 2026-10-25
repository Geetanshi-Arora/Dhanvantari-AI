"""Offline schema tests. All notice identifiers are test-only, not approved notices.

Run with --noconftest: no database fixtures, server, or network are needed.
"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.patients import (
    ConsentDecision,
    ConsentMutationResponse,
    ConsentResponse,
    ConsentRevocationRequest,
    ConsentUpdateRequest,
    PatientDirectoryItem,
    PatientDirectoryResponse,
    PatientRegistrationRequest,
    PatientRegistrationResponse,
    RegistrationStatusResponse,
)

UID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONTEXT = {
    "approved_consent_notices": frozenset(
        (purpose, "test-only-v1", language)
        for purpose in ("care", "uploads", "future_ai")
        for language in ("en", "hi")
    )
}


def decision(purpose="care", granted=True):
    return dict(purpose=purpose, granted=granted, notice_version="test-only-v1", language="en")


def registration():
    return dict(name=" Asha Fictional ", age=36, preferred_language="hi", consents=[decision()])


def response(purpose="care", granted=True, revoked=False):
    return dict(
        id=UID,
        purpose=purpose,
        granted=granted,
        status="revoked" if revoked else ("granted" if granted else "declined"),
        notice_version="historical-test-only",
        language="en",
        created_at=NOW,
        revoked_at=NOW if revoked else None,
    )


def validate(payload):
    return PatientRegistrationRequest.model_validate(payload, context=CONTEXT)


def test_valid_adult_and_optional_decisions_remain_absent():
    result = validate(registration())
    assert result.name == "Asha Fictional"
    assert [d.purpose for d in result.consents] == ["care"]
    assert result.model_dump()["consents"][0]["granted"] is True
    assert result.preferred_language == "hi"
    assert result.consents[0].language == "en"


@pytest.mark.parametrize("age", [18, 120])
def test_adult_boundaries(age):
    assert validate({**registration(), "age": age}).age == age


@pytest.mark.parametrize("age", [17, 121, -1, True, 18.0, "18", None])
def test_invalid_age(age):
    with pytest.raises(ValidationError):
        validate({**registration(), "age": age})


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", "   "),
        ("name", "x" * 81),
        ("name", 123),
        ("preferred_language", "xx"),
        ("sex", "unknown"),
        ("contact_reference", "x" * 61),
        ("history", "x" * 1001),
        ("allergies", "x" * 501),
        ("current_medicines", "x" * 501),
        ("history", 123),
    ],
)
def test_invalid_fields(field, value):
    with pytest.raises(ValidationError):
        validate({**registration(), field: value})


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "reference",
        "created_at",
        "age_recorded_on",
        "registered_by",
        "fictional",
        "representative",
        "guardian",
        "visit_date",
    ],
)
def test_server_controlled_and_unknown_fields_rejected(field):
    with pytest.raises(ValidationError):
        validate({**registration(), field: "untrusted"})


@pytest.mark.parametrize("field", ["name", "age", "preferred_language", "consents"])
def test_required_fields(field):
    payload = registration()
    del payload[field]
    with pytest.raises(ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    "consents",
    [
        [],
        [decision("uploads")],
        [decision(), decision()],
        [decision(), decision("uploads"), decision("uploads")],
    ],
)
def test_missing_care_and_duplicate_purposes(consents):
    with pytest.raises(ValidationError):
        validate({**registration(), "consents": consents})


def test_decline_and_explicit_optional_decisions():
    result = validate(
        {
            **registration(),
            "consents": [
                decision(granted=False),
                decision("uploads", False),
                decision("future_ai", True),
            ],
        }
    )
    assert [d.granted for d in result.consents] == [False, False, True]


@pytest.mark.parametrize("field", ["history", "allergies", "current_medicines"])
def test_declined_care_blocks_clinical_information(field):
    with pytest.raises(ValidationError):
        validate({**registration(), "consents": [decision(granted=False)], field: "Fictional"})


def test_blank_optional_text_normalizes_and_granted_clinical_text_is_bounded():
    result = validate(
        {
            **registration(),
            "history": "x" * 1000,
            "allergies": "x" * 500,
            "current_medicines": "x" * 500,
            "contact_reference": " ",
        }
    )
    assert len(result.history) == 1000
    assert result.contact_reference is None
    assert (
        validate({**registration(), "consents": [decision(granted=False)], "history": " "}).history
        is None
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"granted": "false"},
        {"granted": 0},
        {"granted": None},
        {"purpose": "marketing"},
        {"notice_version": ""},
        {"notice_version": "x" * 41},
        {"notice_version": "unapproved"},
        {"language": "ta"},
        {"representative": "Fictional"},
    ],
)
def test_invalid_or_unapproved_consent(changes):
    with pytest.raises(ValidationError):
        ConsentDecision.model_validate({**decision(), **changes}, context=CONTEXT)


@pytest.mark.parametrize("context", [None, {}, {"approved_consent_notices": frozenset()}])
def test_notice_approval_fails_closed(context):
    with pytest.raises(ValidationError):
        PatientRegistrationRequest.model_validate(registration(), context=context)


def test_granted_must_be_explicit_and_approval_is_purpose_specific():
    payload = decision()
    del payload["granted"]
    with pytest.raises(ValidationError):
        ConsentDecision.model_validate(payload, context=CONTEXT)
    with pytest.raises(ValidationError):
        ConsentDecision.model_validate(
            decision("uploads"),
            context={"approved_consent_notices": frozenset({("care", "test-only-v1", "en")})},
        )


def test_updates_require_explicit_expected_current_id():
    for expected in (None, UID):
        result = ConsentUpdateRequest.model_validate(
            {**decision(), "expected_current_id": expected}, context=CONTEXT
        )
        assert result.expected_current_id == expected
    with pytest.raises(ValidationError):
        ConsentUpdateRequest.model_validate(decision(), context=CONTEXT)
    assert ConsentRevocationRequest(purpose="care").purpose == "care"
    with pytest.raises(ValidationError):
        ConsentRevocationRequest(purpose="care", granted=True)


@pytest.mark.parametrize("granted,revoked", [(True, False), (False, False), (True, True)])
def test_historical_responses_do_not_require_current_notice_approval(granted, revoked):
    assert ConsentResponse(**response(granted=granted, revoked=revoked)).granted == granted


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "declined"},
        {"revoked_at": NOW},
        {"created_at": datetime(2026, 1, 1)},
        {"status": "revoked", "revoked_at": datetime(2025, 1, 1, tzinfo=UTC)},
    ],
)
def test_inconsistent_response_rejected(changes):
    with pytest.raises(ValidationError):
        ConsentResponse(**{**response(), **changes})


@pytest.mark.parametrize("model", [PatientRegistrationResponse, RegistrationStatusResponse])
def test_registration_status_minimal_and_consistent(model):
    payload = dict(
        id=UID,
        reference="FICTIONAL-001",
        preferred_language="hi",
        created_at=NOW,
        care_access="blocked",
        consents=[response(granted=False)],
    )
    result = model(**payload)
    assert "name" not in result.model_dump()
    for change in ({"care_access": "enabled"}, {"consents": []}, {"name": "Fictional"}):
        with pytest.raises(ValidationError):
            model(**{**payload, **change})
    duplicate = deepcopy(payload)
    duplicate["consents"] *= 2
    with pytest.raises(ValidationError):
        model(**duplicate)


def test_directory_date_limits_and_unknown_fields():
    item = dict(
        id=UID,
        reference="FICTIONAL-001",
        name="Fictional",
        age=18,
        age_recorded_on="2026-01-01",
        preferred_language="en",
    )
    assert PatientDirectoryResponse(items=[item], next_cursor=None).items[0].age == 18
    with pytest.raises(ValidationError):
        PatientDirectoryItem(**{**item, "age_recorded_on": "2026-02-30"})
    with pytest.raises(ValidationError):
        PatientDirectoryItem(**{**item, "history": "Fictional"})
    with pytest.raises(ValidationError):
        PatientDirectoryResponse(items=[item] * 101, next_cursor=None)
    with pytest.raises(ValidationError):
        PatientDirectoryResponse(items=[], next_cursor="x" * 513)


def test_mutation_cannot_enable_care_after_revocation():
    revoked = response(revoked=True)
    assert ConsentMutationResponse(consent=revoked, care_access="blocked")
    with pytest.raises(ValidationError):
        ConsentMutationResponse(consent=revoked, care_access="enabled")


def test_error_string_does_not_echo_input():
    marker = "FICTIONAL-PRIVATE-MARKER"
    with pytest.raises(ValidationError) as caught:
        validate({**registration(), "age": marker})
    assert marker not in str(caught.value)


@pytest.mark.parametrize("json_input", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "name",
        "preferred_language",
        "sex",
        "contact_reference",
        "history",
        "allergies",
        "current_medicines",
    ],
)
def test_registration_rejects_nul_without_echoing_input(field, json_input):
    marker = "FICTIONAL-PRIVATE-MARKER"
    payload = {**registration(), field: marker + "\x00"}
    with pytest.raises(ValidationError) as caught:
        if json_input:
            PatientRegistrationRequest.model_validate_json(json.dumps(payload), context=CONTEXT)
        else:
            validate(payload)
    assert marker not in str(caught.value)
    assert "NUL characters are not permitted" in str(caught.value)


@pytest.mark.parametrize(
    "model,field",
    [
        (model, field)
        for model in (ConsentDecision, ConsentUpdateRequest, ConsentResponse)
        for field in ("purpose", "notice_version", "language")
    ]
    + [(ConsentRevocationRequest, "purpose")],
)
def test_consent_text_rejects_nul(model, field):
    payload = response() if model is ConsentResponse else decision()
    if model is ConsentUpdateRequest:
        payload["expected_current_id"] = None
    if model is ConsentRevocationRequest:
        payload = {"purpose": "care"}
    payload[field] = "Fictional\x00"
    # Even a mistakenly configured catalogue cannot approve PostgreSQL NUL text.
    context = {
        "approved_consent_notices": frozenset(
            {(payload.get("purpose"), payload.get("notice_version"), payload.get("language"))}
        )
    }
    with pytest.raises(ValidationError, match="NUL characters are not permitted"):
        model.model_validate(payload, context=context)


@pytest.mark.parametrize("field", ["name", "reference", "preferred_language"])
def test_directory_text_rejects_nul(field):
    payload = dict(
        id=UID,
        reference="FICTIONAL-001",
        name="Fictional",
        age=18,
        age_recorded_on="2026-01-01",
        preferred_language="en",
    )
    with pytest.raises(ValidationError, match="NUL characters are not permitted"):
        PatientDirectoryItem.model_validate({**payload, field: "Fictional\x00"})


@pytest.mark.parametrize("model", [RegistrationStatusResponse, PatientRegistrationResponse])
def test_registration_response_reference_rejects_nul(model):
    payload = dict(
        id=UID,
        reference="Fictional\x00",
        preferred_language="en",
        created_at=NOW,
        care_access="enabled",
        consents=[response()],
    )
    with pytest.raises(ValidationError, match="NUL characters are not permitted"):
        model.model_validate(payload)


def test_json_registration_matches_dictionary_and_omits_optional_decisions():
    payload = registration()
    result = PatientRegistrationRequest.model_validate_json(json.dumps(payload), context=CONTEXT)
    assert result == validate(payload)
    assert [consent.purpose for consent in result.consents] == ["care"]
    assert "uploads" not in [c["purpose"] for c in result.model_dump()["consents"]]
    assert "future_ai" not in [c["purpose"] for c in result.model_dump()["consents"]]


@pytest.mark.parametrize("context", [None, {}, {"approved_consent_notices": frozenset()}])
def test_json_registration_missing_approval_fails_closed(context):
    with pytest.raises(ValidationError):
        PatientRegistrationRequest.model_validate_json(json.dumps(registration()), context=context)


@pytest.mark.parametrize("model", [PatientRegistrationRequest, ConsentUpdateRequest])
def test_json_unapproved_translation_matches_dictionary_rejection(model):
    payload = (
        registration()
        if model is PatientRegistrationRequest
        else {
            **decision(),
            "expected_current_id": None,
        }
    )
    target = payload["consents"][0] if model is PatientRegistrationRequest else payload
    target["language"] = "ta"
    for method, value in (
        (model.model_validate, payload),
        (model.model_validate_json, json.dumps(payload)),
    ):
        with pytest.raises(ValidationError, match="not approved"):
            method(value, context=CONTEXT)


@pytest.mark.parametrize("expected", [None, str(UID)])
def test_json_consent_update_with_approved_context(expected):
    payload = {**decision(), "expected_current_id": expected}
    result = ConsentUpdateRequest.model_validate_json(json.dumps(payload), context=CONTEXT)
    assert result == ConsentUpdateRequest.model_validate(payload, context=CONTEXT)


@pytest.mark.parametrize("context", [None, {}, {"approved_consent_notices": frozenset()}])
def test_json_consent_update_without_approval_fails_closed(context):
    payload = {**decision(), "expected_current_id": None}
    with pytest.raises(ValidationError):
        ConsentUpdateRequest.model_validate_json(json.dumps(payload), context=context)


@pytest.mark.parametrize(
    "changes",
    [
        {"age": True},
        {"age": "18"},
        {"id": str(UID)},
        {"consents": [decision(granted=False)], "history": "Fictional history"},
        {"consents": [decision(), decision()]},
    ],
)
def test_json_registration_preserves_core_validation(changes):
    payload = {**registration(), **changes}
    for method, value in (
        (PatientRegistrationRequest.model_validate, payload),
        (PatientRegistrationRequest.model_validate_json, json.dumps(payload)),
    ):
        with pytest.raises(ValidationError):
            method(value, context=CONTEXT)
