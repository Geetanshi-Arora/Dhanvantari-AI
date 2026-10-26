"""Fictional NON-APPROVED test data only; flags simulate approval for unit tests.

No fixture is a human approval, real notice, translation or production catalogue.
Run with --noconftest; no network, database or application startup is needed.
"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from app.schemas.consent_notices import (
    PRODUCTION_NOTICE_CATALOGUE,
    NoticeArtifact,
    NoticeCatalogue,
    NoticeIdentity,
    NoticeReadiness,
    notice_readiness,
)
from pydantic import ValidationError

NOW = datetime(2026, 1, 2, tzinfo=UTC)
TEXT = "NON-APPROVED FICTIONAL TEST CONTENT ONLY.\nUnicode fixture: नमूना\n"
VERSION = "fixture-only-not-approved"


def payload(**changes):
    return (
        dict(
            purpose="care",
            notice_version=VERSION,
            language="en",
            text=TEXT,
            content_hash=sha256(TEXT.encode("utf-8")).hexdigest(),
            approval_references=["fictional-evidence-NOT-human-authorization"],
            approved=True,
            approved_at=NOW.isoformat(),
            retired=False,
            retired_at=None,
        )
        | changes
    )


def artifact(**changes):
    return NoticeArtifact.model_validate(payload(**changes))


def ready(catalogue=PRODUCTION_NOTICE_CATALOGUE, **changes):
    return notice_readiness(
        changes.pop("purpose", "care"),
        changes.pop("notice_version", VERSION),
        changes.pop("language", "en"),
        at=changes.pop("at", NOW),
        catalogue=catalogue,
        **changes,
    )


def test_valid_python_and_json_keep_exact_text_and_hash():
    a = artifact()
    b = NoticeArtifact.model_validate_json(json.dumps(payload()))
    assert a == b and a.text == TEXT
    assert a.content_hash == sha256(TEXT.encode()).hexdigest()
    assert ready(NoticeCatalogue(artifacts=(a,))).available


@pytest.mark.parametrize("text", [TEXT.rstrip(), TEXT.replace("\n", "\r\n"), "é", "e\u0301"])
def test_hash_binds_exact_text_without_normalization(text):
    with pytest.raises(ValidationError, match="content hash mismatch"):
        artifact(text=text)
    exact = artifact(text=text, content_hash=sha256(text.encode("utf-8")).hexdigest())
    assert exact.text == text


def test_json_catalogue_retains_history_and_rejects_conflicts():
    catalogue = NoticeCatalogue.model_validate_json(json.dumps({"artifacts": [payload()]}))
    assert catalogue.historical(artifact().identity).text == TEXT
    with pytest.raises(ValidationError, match="Duplicate notice identity"):
        NoticeCatalogue.model_validate_json(json.dumps({"artifacts": [payload(), payload()]}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("purpose", "diagnosis"),
        ("language", "fr"),
        ("language", "EN"),
        ("notice_version", ""),
        ("notice_version", " " * 3),
        ("notice_version", "x" * 41),
        ("notice_version", 1),
        ("text", ""),
        ("text", " \n"),
        ("text", TEXT + "changed"),
        ("text", "x" * 65537),
        ("text", "\ud800"),
        ("content_hash", "0" * 64),
        ("content_hash", "a" * 63),
        ("content_hash", "A" * 64),
        ("approved", "true"),
        ("approval_references", []),
        ("approval_references", [""]),
        ("approval_references", ["  "]),
        ("approval_references", ["x" * 201]),
        ("approval_references", ["a", "a"]),
        ("approval_references", ["a\x00b"]),
        ("approved_at", None),
        ("approved_at", "2026-01-02T00:00:00"),
        ("retired_at", NOW.isoformat()),
        ("retired", True),
        ("unknown", "FORBIDDEN"),
    ],
)
def test_invalid_artifacts(field, value):
    with pytest.raises(ValidationError):
        artifact(**{field: value})


@pytest.mark.parametrize("field", ["text", "notice_version", "language", "purpose"])
def test_nul_rejected_with_sanitized_error(field):
    secret = "PRIVATE-SUBMITTED-TEXT\x00"
    with pytest.raises(ValidationError) as error:
        artifact(**{field: secret})
    assert secret not in str(error.value)
    assert "PRIVATE-SUBMITTED-TEXT" not in str(error.value)


@pytest.mark.parametrize(
    "field", ["approval_references", "approved", "approved_at", "retired", "retired_at"]
)
def test_metadata_must_be_explicit(field):
    data = payload()
    del data[field]
    with pytest.raises(ValidationError):
        NoticeArtifact.model_validate(data)


@pytest.mark.parametrize("language", ["en", "hi", "bn", "ta", "te", "mr", "gu", "pa"])
def test_eight_language_codes_supported_without_claiming_real_translations(language):
    assert artifact(language=language).language == language


@pytest.mark.parametrize("purpose", ["care", "uploads", "future_ai"])
def test_supported_purposes(purpose):
    assert artifact(purpose=purpose).purpose == purpose


@pytest.mark.parametrize("version", ["x", "x" * 40])
def test_version_bounds(version):
    assert artifact(notice_version=version).notice_version == version


@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_tuple_rejected_even_with_a_valid_different_hash(changed):
    first = artifact()
    text = TEXT + "changed" if changed else TEXT
    second = artifact(text=text, content_hash=sha256(text.encode()).hexdigest())
    with pytest.raises(ValidationError, match="Duplicate notice identity"):
        NoticeCatalogue(artifacts=(first, second))


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"approved": False, "approved_at": None}, "unapproved"),
        ({"retired": True, "retired_at": (NOW + timedelta(days=1)).isoformat()}, "retired"),
        ({"approved_at": (NOW + timedelta(days=1)).isoformat()}, "not_yet_approved"),
    ],
)
def test_unavailable_for_new_decisions(changes, reason):
    result = ready(NoticeCatalogue(artifacts=(artifact(**changes),)))
    assert not result.available and result.reason == reason


def test_bad_retirement_chronology_and_unapproved_retirement_rejected():
    with pytest.raises(ValidationError):
        artifact(retired=True, retired_at=(NOW - timedelta(days=1)).isoformat())
    with pytest.raises(ValidationError):
        artifact(approved=False, approved_at=None, retired=True, retired_at=NOW)


def test_no_language_version_or_purpose_fallback():
    catalogue = NoticeCatalogue(artifacts=(artifact(),))
    for changes in (
        {"language": "hi"},
        {"notice_version": "other-fixture"},
        {"purpose": "uploads"},
    ):
        result = ready(catalogue, **changes)
        assert not result.available and result.reason == "missing"
    unapproved = artifact(language="hi", approved=False, approved_at=None)
    assert (
        ready(NoticeCatalogue(artifacts=(artifact(), unapproved)), language="hi").reason
        == "unapproved"
    )


def test_history_keeps_exact_identity_and_text_after_retirement():
    old = artifact(retired=True, retired_at=NOW)
    new = artifact(notice_version="different-fixture-only")
    catalogue = NoticeCatalogue(artifacts=(old, new))
    assert catalogue.historical(old.identity) == old
    assert catalogue.historical(old.identity).content_hash == old.content_hash
    assert not ready(catalogue).available
    assert ready(catalogue, notice_version=new.notice_version).available


def test_immutable_nested_models_and_sequences():
    a = artifact()
    catalogue = NoticeCatalogue(artifacts=[a])
    with pytest.raises(ValidationError):
        a.text = "changed"
    with pytest.raises(ValidationError):
        catalogue.artifacts = ()
    assert isinstance(catalogue.artifacts, tuple)
    assert isinstance(a.approval_references, tuple)


@pytest.mark.parametrize(
    "changes", [{"text": "changed"}, {"approved": False}, {"content_hash": "0" * 64}]
)
def test_unvalidated_model_copy_cannot_bypass_readiness(changes):
    forged = artifact().model_copy(update=changes)
    catalogue = NoticeCatalogue.model_construct(artifacts=(forged,))
    assert ready(catalogue).reason == "invalid"


@pytest.mark.parametrize(
    "changes",
    [
        {"language": "fr"},
        {"purpose": "wrong"},
        {"notice_version": ""},
        {"at": NOW.replace(tzinfo=None)},
        {"at": "not-a-date"},
    ],
)
def test_invalid_readiness_inputs_fail_closed(changes):
    assert not ready(**changes).available
    assert ready(**changes).reason == "invalid"


def test_defaults_and_invalid_catalogues_fail_closed():
    assert PRODUCTION_NOTICE_CATALOGUE.artifacts == ()
    assert not NoticeReadiness().available
    assert ready().reason == "missing"
    assert ready(None).reason == "invalid"
    with pytest.raises(ValidationError):
        NoticeReadiness(available=True)
    with pytest.raises(ValidationError):
        NoticeIdentity(purpose="care", notice_version=VERSION, language="en", actor="forbidden")
