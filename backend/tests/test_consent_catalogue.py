"""Offline fictional, NON-APPROVED fixtures; simulated flags are not human approval."""

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from app.schemas.consent_notices import (
    PRODUCTION_NOTICE_CATALOGUE,
    NoticeArtifact,
    NoticeCatalogue,
    NoticeReadiness,
)
from app.services.consent_catalogue import (
    PRODUCTION_CONSENT_ADAPTER,
    ConsentCatalogueAdapter,
)
from pydantic import ValidationError

NOW = datetime(2026, 1, 2, tzinfo=UTC)
VERSION = "fixture-not-human-approved"
TEXT = "FICTIONAL NON-APPROVED UNIT TEST CONTENT ONLY.\n"


def artifact(**changes):
    return NoticeArtifact.model_validate(
        dict(
            purpose="care",
            notice_version=VERSION,
            language="en",
            text=TEXT,
            content_hash=sha256(TEXT.encode()).hexdigest(),
            approval_references=["fictional-reference-not-human-evidence"],
            approved=True,
            approved_at=NOW,
            retired=False,
            retired_at=None,
        )
        | changes
    )


def adapter(*artifacts):
    return ConsentCatalogueAdapter(NoticeCatalogue(artifacts=artifacts))


def lookup(subject, **changes):
    return subject.historical_lookup(
        changes.get("purpose", "care"),
        changes.get("notice_version", VERSION),
        changes.get("language", "en"),
    )


def readiness(subject, **changes):
    return subject.new_decision_readiness(
        changes.get("purpose", "care"),
        changes.get("notice_version", VERSION),
        changes.get("language", "en"),
        at=changes.get("at", NOW),
    )


def test_production_and_default_adapter_are_empty_and_fail_closed():
    for subject in [ConsentCatalogueAdapter(), PRODUCTION_CONSENT_ADAPTER]:
        assert subject.catalogue.artifacts == ()
        assert lookup(subject) is None
        assert not subject.historical_member("care", VERSION, "en")
        assert readiness(subject).reason == "missing"
        assert not readiness(subject).available
    assert PRODUCTION_NOTICE_CATALOGUE.artifacts == ()


def test_exact_historical_lookup_preserves_text_hash_and_identity():
    original = artifact()
    found = lookup(adapter(original))
    assert found == original
    assert found.identity == original.identity
    assert found.text == TEXT and found.content_hash == sha256(TEXT.encode()).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"language": "hi"},
        {"notice_version": "missing-fixture"},
        {"purpose": "uploads"},
    ],
)
def test_no_substitution_on_lookup_or_new_readiness(changes):
    subject = adapter(artifact())
    assert lookup(subject, **changes) is None
    assert readiness(subject, **changes).reason == "missing"
    assert not readiness(subject, **changes).available


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"retired": True, "retired_at": NOW}, "retired"),
        ({"approved": False, "approved_at": None}, "unapproved"),
        ({"approved_at": NOW + timedelta(days=1)}, "not_yet_approved"),
    ],
)
def test_historical_presence_cannot_authorize_new_decisions(changes, reason):
    original = artifact(**changes)
    subject = adapter(original)
    assert lookup(subject) == original
    assert subject.historical_member("care", VERSION, "en")
    assert readiness(subject).reason == reason
    assert not readiness(subject).available


def test_missing_and_unapproved_translation_never_fall_back_to_english():
    subject = adapter(artifact(), artifact(language="hi", approved=False, approved_at=None))
    assert readiness(subject).available
    assert readiness(subject, language="hi").reason == "unapproved"
    assert readiness(subject, language="ta").reason == "missing"


@pytest.mark.parametrize("different_text", [False, True])
def test_conflicting_or_identical_duplicates_rejected_at_adapter_boundary(different_text):
    text = TEXT + "DIFFERENT FIXTURE" if different_text else TEXT
    second = artifact(text=text, content_hash=sha256(text.encode()).hexdigest())
    forged = NoticeCatalogue.model_construct(artifacts=(artifact(), second))
    with pytest.raises(ValidationError, match="Duplicate notice identity"):
        ConsentCatalogueAdapter(forged)


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "ALTERED"},
        {"content_hash": "0" * 64},
        {"approval_references": ()},
        {"approved_at": None},
    ],
)
def test_invalid_nested_artifacts_rejected_before_adapter_is_usable(changes):
    forged = artifact().model_copy(update=changes)
    with pytest.raises(ValidationError):
        ConsentCatalogueAdapter(NoticeCatalogue.model_construct(artifacts=(forged,)))


@pytest.mark.parametrize("value", [None, {}, (), "operator-file"])
def test_unknown_catalogue_inputs_rejected(value):
    with pytest.raises(ValueError, match="Invalid notice catalogue"):
        ConsentCatalogueAdapter(value)


def test_catalogue_snapshot_is_immutable_and_detached_from_input_sequence():
    items = [artifact()]
    subject = ConsentCatalogueAdapter(NoticeCatalogue(artifacts=items))
    items.clear()
    assert len(subject.catalogue.artifacts) == 1
    with pytest.raises(FrozenInstanceError):
        subject.catalogue = NoticeCatalogue()
    with pytest.raises(ValidationError):
        subject.catalogue.artifacts = ()
    with pytest.raises(ValidationError):
        lookup(subject).retired = True


@pytest.mark.parametrize(
    "changes", [{"language": "fr"}, {"notice_version": ""}, {"purpose": "unknown"}]
)
def test_invalid_identity_never_becomes_a_historical_match(changes):
    subject = adapter(artifact())
    with pytest.raises(ValidationError):
        lookup(subject, **changes)
    assert readiness(subject, **changes).reason == "invalid"


@pytest.mark.parametrize(
    "reason",
    [
        "missing",
        "unapproved",
        "retired",
        "not_yet_approved",
        "invalid",
        "available",
    ],
)
def test_explicit_validation_payload_round_trip_preserves_derived_availability(reason):
    identity = artifact().identity
    original = NoticeReadiness(identity=identity, reason=reason)
    payload = original.validation_payload()
    assert "available" not in payload
    assert payload["identity"] == identity.model_dump(mode="json")
    assert NoticeReadiness.model_validate(payload) == original
    restored = NoticeReadiness.model_validate_json(json.dumps(payload))
    assert restored == original and restored.available == original.available
    # Normal output still includes convenience availability, never trusted as input.
    assert original.model_dump(mode="json")["available"] == original.available
    with pytest.raises(ValidationError):
        NoticeReadiness.model_validate(original.model_dump())
    with pytest.raises(ValidationError):
        NoticeReadiness.model_validate(payload | {"available": True})
    with pytest.raises(ValidationError):
        NoticeReadiness.model_validate_json(json.dumps(payload | {"unknown": "FORBIDDEN"}))


def test_default_readiness_payload_round_trips_without_identity():
    original = NoticeReadiness()
    restored = NoticeReadiness.model_validate_json(json.dumps(original.validation_payload()))
    assert restored == original and not restored.available


def test_adapter_exposes_no_replay_or_patient_authorization_operation():
    subject = adapter(artifact(retired=True, retired_at=NOW))
    assert subject.historical_member("care", VERSION, "en")
    assert not readiness(subject).available
    assert not hasattr(subject, "authorize_retry")
    assert not hasattr(subject, "register_patient")
    assert not hasattr(subject, "approved_consent_notices")
