"""Offline orchestration tests. Every identity, permission and provider is fictional.

No fixtures from conftest, SQL, audio files or actual provider connections are used.
"""

import gc
import socket
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.security import Principal
from app.schemas.voice import (
    GNANI_CAPABILITIES,
    PlaybackMetadata,
    SpeechCapability,
    SpeechToTextRequest,
    TextToSpeechRequest,
    TranscriptDraft,
    TranscriptReviewAction,
    VoiceCapabilities,
)
from app.services.voice import VoiceAuthorization, VoiceService, VoiceServiceError

NOW = datetime(2026, 1, 2, tzinfo=UTC)
TEXT = "FICTIONAL UNVERIFIED TRANSCRIPT ONLY"
CORRECTED = "FICTIONAL NURSE CORRECTION ONLY"
AUDIO = b"FICTIONAL-NON-AUDIO-FAKE-PROVIDER-INPUT"
MEDIA = "audio/x-fictional-test"
SUPPORT = SpeechCapability(
    available=True, languages=frozenset({"hi"}), media_types=frozenset({MEDIA})
)
CAPS = VoiceCapabilities(provider="fictional_offline", stt=SUPPORT, tts=SUPPORT)
CONTEXT = {"voice_capabilities": CAPS}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network forbidden in offline voice tests")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


class FakeProvider:
    capabilities = CAPS

    def __init__(self):
        self.calls = 0
        self.hook = lambda: None
        self.change = lambda result: result

    def transcribe(self, *, audio, filename, request):
        self.calls += 1
        assert audio == AUDIO
        self.hook()
        return self.change(
            TranscriptDraft(
                **identity(request), provider=CAPS.provider, original_text=TEXT, created_at=NOW
            )
        )

    def synthesize(self, *, request):
        self.calls += 1
        self.hook()
        return self.change(
            SimpleNamespace(
                audio=AUDIO,
                metadata=PlaybackMetadata(
                    **identity(request),
                    provider=CAPS.provider,
                    media_type=MEDIA,
                    byte_count=len(AUDIO),
                    duration_ms=1000,
                ),
            )
        )


def identity(request):
    return {
        key: getattr(request, key) for key in ("request_id", "patient_id", "visit_id", "language")
    }


class Rig:
    def __init__(self):
        self.principal = Principal(uuid4(), "Fictional Nurse", "nurse", "a" * 64)
        self.request = SpeechToTextRequest.model_validate(
            dict(
                request_id=uuid4(),
                patient_id=uuid4(),
                visit_id=uuid4(),
                language="hi",
                media_type=MEDIA,
                byte_count=len(AUDIO),
                duration_ms=1000,
            ),
            context=CONTEXT,
        )
        self.decision = VoiceAuthorization(
            **identity(self.request),
            actor_id=self.principal.id,
            session_hash=self.principal.session_hash,
            context_revision=uuid4(),
            assignment_revision=uuid4(),
            care_revision=uuid4(),
            voice_revision=uuid4(),
            authenticated=True,
            active_nurse=True,
            patient_assigned=True,
            care_granted=True,
            fictional_patient=True,
            voice_notice_approved=True,
            recording_permitted=True,
            external_voice_processing_permitted=True,
        )
        self.events = []
        self.authorizations = []
        self.auth_hook = lambda: None
        self.audit_hook = lambda event: None
        self.provider = FakeProvider()
        self.service = VoiceService(
            authorizer=self.authorize,
            auditor=self.audit,
            stt=self.provider,
            tts=self.provider,
            capabilities=CAPS,
            enabled=True,
            clock=lambda: NOW,
        )

    def authorize(self, **kwargs):
        self.authorizations.append(kwargs)
        self.auth_hook()
        return self.decision

    def audit(self, **event):
        self.audit_hook(event)
        self.events.append(event)

    def change(self, **changes):
        self.decision = self.decision.model_copy(update=changes)

    def transcribe(self, **changes):
        return self.service.transcribe(
            self.principal, self.request, **(dict(audio=AUDIO, filename="fictional.wav") | changes)
        )

    def tts(self):
        request = TextToSpeechRequest.model_validate(
            identity(self.request) | dict(text=CORRECTED, media_type=MEDIA),
            context=CONTEXT,
        )
        self.change(
            synthesis_permitted=True,
            tts_reviewed_by=self.principal.id,
            tts_text_digest=sha256(CORRECTED.encode()).hexdigest(),
        )
        return request


@pytest.fixture
def rig():
    return Rig()


def fails(code, operation):
    with pytest.raises(VoiceServiceError) as caught:
        operation()
    assert caught.value.code == code
    assert TEXT not in str(caught.value) and CORRECTED not in str(caught.value)
    assert "PRIVATE_SECRET" not in str(caught.value)
    return caught.value


def test_default_disabled_and_unavailable(rig):
    assert not GNANI_CAPABILITIES.stt.available and not GNANI_CAPABILITIES.tts.available
    rig.service._enabled = False
    fails("disabled", rig.transcribe)
    assert rig.provider.calls == 0
    service = VoiceService()
    fails(
        "invalid_request",
        lambda: service.transcribe(rig.principal, rig.request, audio=AUDIO, filename="fake.wav"),
    )


@pytest.mark.parametrize(
    "field",
    [
        "authenticated",
        "active_nurse",
        "patient_assigned",
        "care_granted",
        "fictional_patient",
        "voice_notice_approved",
        "recording_permitted",
        "external_voice_processing_permitted",
    ],
)
def test_every_independent_permission_required_before_egress(rig, field):
    rig.change(**{field: False})
    fails("forbidden", rig.transcribe)
    assert rig.provider.calls == 0


@pytest.mark.parametrize("field", ["actor_id", "patient_id", "visit_id", "request_id"])
def test_identity_mismatch_never_authorizes(rig, field):
    rig.change(**{field: uuid4()})
    fails("forbidden", rig.transcribe)
    assert rig.provider.calls == 0


@pytest.mark.parametrize(
    "change",
    [dict(role="doctor"), dict(role=""), dict(id="not-uuid"), dict(session_hash="invalid")],
)
def test_unverified_principal_shape_rejected(rig, change):
    rig.principal = replace(rig.principal, **change)
    fails("forbidden", rig.transcribe)
    assert not rig.events and rig.provider.calls == 0


def test_missing_authorizer_and_auditor_fail_closed(rig):
    rig.service._authorize_callback = None
    fails("authorization_unavailable", rig.transcribe)
    rig.service._authorize_callback = rig.authorize
    rig.service._audit_callback = None
    fails("audit_unavailable", rig.transcribe)
    assert rig.provider.calls == 0


@pytest.mark.parametrize(
    "field", ["care_granted", "recording_permitted", "external_voice_processing_permitted"]
)
@pytest.mark.parametrize("value", [None, "true", 1])
def test_uncertain_decisions_revalidated_not_truthiness(rig, field, value):
    rig.change(**{field: value})
    fails("authorization_unavailable", rig.transcribe)
    assert rig.provider.calls == 0


@pytest.mark.parametrize(
    "field",
    [
        "authenticated",
        "active_nurse",
        "patient_assigned",
        "care_granted",
        "voice_notice_approved",
        "recording_permitted",
        "external_voice_processing_permitted",
    ],
)
def test_revocation_during_provider_discards_result(rig, field):
    rig.provider.hook = lambda: rig.change(**{field: False})
    fails("stale", rig.transcribe)
    assert rig.provider.calls == 1 and rig.events[-1]["outcome"] == "stale"
    assert not rig.service._handles and not rig.service._inflight


@pytest.mark.parametrize(
    "field",
    [
        "context_revision",
        "assignment_revision",
        "care_revision",
        "voice_revision",
        "patient_id",
        "visit_id",
        "request_id",
        "session_hash",
        "language",
    ],
)
def test_stale_context_and_aba_revision_changes_discard(rig, field):
    value = "b" * 64 if field == "session_hash" else "en" if field == "language" else uuid4()
    rig.provider.hook = lambda: rig.change(**{field: value})
    fails("stale", rig.transcribe)
    assert not rig.service._handles


def test_recheck_after_audit_prevents_returning_revoked_data(rig):
    def revoke(event):
        if event["outcome"] == "success":
            rig.change(care_granted=False)

    rig.audit_hook = revoke
    fails("stale", rig.transcribe)
    assert not rig.service._handles


@pytest.mark.parametrize("stage", ["attempt", "success"])
def test_required_audit_failure_never_returns_content(rig, stage):
    def reject(event):
        if event["outcome"] == stage:
            raise RuntimeError("PRIVATE_SECRET " + TEXT)

    rig.audit_hook = reject
    fails("audit_unavailable", rig.transcribe)
    assert rig.provider.calls == (stage == "success")
    assert not rig.service._handles


def test_audit_only_fixed_metadata_no_text_audio_or_session(rig):
    result = rig.transcribe()
    assert result.transcript.state == "unverified"
    assert len(rig.authorizations) == 4
    for event in rig.events:
        assert set(event) == {"action", "outcome", "request_id", "actor_id", "resource_id"}
        assert event["action"] == "voice.stt"
        assert TEXT not in repr(event) and rig.principal.session_hash not in repr(event)
    assert TEXT not in repr(result)
    assert rig.principal.display_name not in repr(rig.authorizations)


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("PRIVATE_SECRET"), "provider_timeout"),
        (RuntimeError("PRIVATE_SECRET " + TEXT), "provider_failed"),
    ],
)
def test_provider_errors_sanitized_and_reservation_released(rig, error, code):
    def fail():
        raise error

    rig.provider.hook = fail
    fails(code, rig.transcribe)
    assert not rig.service._inflight


def test_authorizer_errors_sanitized(rig):
    def fail():
        raise RuntimeError("PRIVATE_SECRET")

    rig.auth_hook = fail
    fails("authorization_unavailable", rig.transcribe)
    assert rig.provider.calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "nurse_confirmed"},
        {"patient_id": uuid4()},
        {"visit_id": uuid4()},
        {"language": "en"},
        {"provider": "wrong_provider"},
        {"original_text": ""},
        {"original_text": "x" * 8001},
    ],
)
def test_malformed_provider_draft_never_accepted(rig, changes):
    rig.provider.change = lambda draft: draft.model_copy(update=changes)
    fails("invalid_response", rig.transcribe)


def test_review_requires_explicit_actions_and_preserves_history(rig):
    original = rig.transcribe()
    assert original.transcript.state == "unverified" and original.transcript.reviews == ()
    corrected = rig.service.review(
        rig.principal,
        original,
        TranscriptReviewAction(expected_revision=0, action="correct", corrected_text=CORRECTED),
    )
    assert corrected.transcript.state == "nurse_corrected"
    assert original.transcript.current_text == TEXT
    assert corrected.transcript.draft == original.transcript.draft
    confirmed = rig.service.review(
        rig.principal, corrected, TranscriptReviewAction(expected_revision=1, action="confirm")
    )
    assert confirmed.transcript.state == "nurse_confirmed"
    assert confirmed.transcript.current_text == CORRECTED
    assert confirmed.transcript.reviews[:1] == corrected.transcript.reviews
    assert all(event.nurse_id == rig.principal.id for event in confirmed.transcript.reviews)
    with pytest.raises(FrozenInstanceError):
        confirmed.transcript = original.transcript
    with pytest.raises(ValidationError):
        confirmed.transcript.state = "unverified"
    fails(
        "review_conflict",
        lambda: rig.service.review(
            rig.principal, confirmed, TranscriptReviewAction(expected_revision=2, action="confirm")
        ),
    )


def test_forged_old_and_other_service_handles_rejected(rig):
    original = rig.transcribe()
    action = TranscriptReviewAction(expected_revision=0, action="correct", corrected_text=CORRECTED)
    fails("stale", lambda: rig.service.review(rig.principal, replace(original), action))
    other = Rig()
    other.principal, other.decision = rig.principal, rig.decision
    fails("stale", lambda: other.service.review(other.principal, original, action))
    confirmed = rig.service.review(rig.principal, original, action)
    fails("stale", lambda: rig.service.review(rig.principal, original, action))
    assert confirmed.transcript.state == "nurse_corrected"


def test_review_stale_revision_and_revoked_permission(rig):
    original = rig.transcribe()
    fails(
        "review_conflict",
        lambda: rig.service.review(
            rig.principal, original, TranscriptReviewAction(expected_revision=1, action="confirm")
        ),
    )
    rig.change(voice_revision=uuid4())
    fails(
        "stale",
        lambda: rig.service.review(
            rig.principal, original, TranscriptReviewAction(expected_revision=0, action="confirm")
        ),
    )


def test_duplicate_inflight_and_no_lock_held_across_provider(rig):
    def check():
        assert rig.service._lock.acquire(blocking=False)
        rig.service._lock.release()
        fails("stale", rig.transcribe)

    rig.provider.hook = check
    original = rig.transcribe()
    assert original.transcript.state == "unverified" and rig.provider.calls == 1
    fails("stale", rig.transcribe)


def test_service_does_not_retain_transcript_content(rig):
    handle = rig.transcribe()
    assert len(rig.service._handles) == 1
    del handle
    gc.collect()
    assert not rig.service._handles


def test_shutdown_discards_inflight_response(rig):
    rig.provider.hook = rig.service.close
    fails("stale", rig.transcribe)
    assert not rig.service._handles and not rig.service._inflight


@pytest.mark.parametrize(
    "kwargs",
    [
        {"audio": b""},
        {"audio": bytearray(AUDIO)},
        {"audio": b"x" * (4 * 1024 * 1024 + 1)},
        {"audio": b"wrong_count"},
        {"filename": "../fictional.wav"},
        {"filename": "https://evil.test/a.wav"},
    ],
)
def test_bounded_audio_and_safe_names_before_provider(rig, kwargs):
    fails("invalid_request", lambda: rig.transcribe(**kwargs))
    assert rig.provider.calls == 0


def test_unsupported_language_and_independent_capabilities(rig):
    rig.request = rig.request.model_copy(update={"language": "en"})
    fails("invalid_request", rig.transcribe)
    assert rig.provider.calls == 0
    rig.request = rig.request.model_copy(update={"language": "hi"})
    rig.provider.capabilities = VoiceCapabilities(provider=CAPS.provider, tts=SUPPORT)
    fails("unsupported", rig.transcribe)


def test_capability_change_inflight_discards(rig):
    rig.provider.hook = lambda: setattr(rig.provider, "capabilities", GNANI_CAPABILITIES)
    fails("stale", rig.transcribe)


def test_tts_requires_exact_separately_reviewed_text_no_automatic_playback(rig):
    request = rig.tts()
    rig.change(recording_permitted=False)
    result = rig.service.synthesize(rig.principal, request)
    assert result.audio == AUDIO and result.playback_authorized is False
    assert CORRECTED not in repr(result)
    assert rig.authorizations[0]["text_digest"] == sha256(CORRECTED.encode()).hexdigest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("synthesis_permitted", False),
        ("tts_text_digest", None),
        ("tts_text_digest", "0" * 64),
        ("tts_reviewed_by", None),
        ("tts_reviewed_by", uuid4()),
        ("care_granted", False),
        ("external_voice_processing_permitted", False),
        ("voice_notice_approved", False),
    ],
)
def test_tts_missing_review_or_separate_permission_never_calls_provider(rig, field, value):
    request = rig.tts()
    rig.change(**{field: value})
    fails("forbidden", lambda: rig.service.synthesize(rig.principal, request))
    assert rig.provider.calls == 0


def test_tts_permission_revocation_during_response(rig):
    request = rig.tts()
    rig.provider.hook = lambda: rig.change(tts_text_digest="b" * 64)
    fails("stale", lambda: rig.service.synthesize(rig.principal, request))


@pytest.mark.parametrize(
    "change",
    [
        lambda result: SimpleNamespace(audio=b"", metadata=result.metadata),
        lambda result: SimpleNamespace(
            audio=b"x" * (4 * 1024 * 1024 + 1), metadata=result.metadata
        ),
        lambda result: SimpleNamespace(
            audio=AUDIO, metadata=result.metadata.model_copy(update={"byte_count": 1})
        ),
        lambda result: SimpleNamespace(
            audio=AUDIO, metadata=result.metadata.model_copy(update={"visit_id": uuid4()})
        ),
        lambda result: None,
    ],
)
def test_tts_malformed_result_rejected(rig, change):
    request = rig.tts()
    rig.provider.change = change
    fails("invalid_response", lambda: rig.service.synthesize(rig.principal, request))


def test_authorization_strict_immutable_default_denial(rig):
    minimal = VoiceAuthorization.model_validate(
        {
            key: value
            for key, value in rig.decision.model_dump().items()
            if key.endswith("_revision")
            or key
            in {"request_id", "patient_id", "visit_id", "language", "actor_id", "session_hash"}
        }
    )
    assert not minimal.care_granted and not minimal.recording_permitted
    with pytest.raises(ValidationError):
        minimal.care_granted = True
    with pytest.raises(ValidationError):
        VoiceAuthorization.model_validate(rig.decision.model_dump() | {"care": True})


def test_confirmation_requires_separate_explicit_nurse_text_submission(rig):
    original = rig.transcribe()
    fails(
        "review_conflict",
        lambda: rig.service.review(
            rig.principal, original, TranscriptReviewAction(expected_revision=0, action="confirm")
        ),
    )
    corrected = rig.service.review(
        rig.principal,
        original,
        TranscriptReviewAction(expected_revision=0, action="correct", corrected_text=TEXT),
    )
    confirmed = rig.service.review(
        rig.principal, corrected, TranscriptReviewAction(expected_revision=1, action="confirm")
    )
    assert confirmed.transcript.current_text == TEXT
    assert confirmed.transcript.state == "nurse_confirmed"
    assert len(confirmed.transcript.reviews) == 2


def test_authorization_is_rechecked_before_provider_after_attempt_audit(rig):
    def revoke(event):
        if event["outcome"] == "attempt":
            rig.change(recording_permitted=False)

    rig.audit_hook = revoke
    fails("stale", rig.transcribe)
    assert rig.provider.calls == 0


def test_review_context_change_during_audit_discards_new_revision(rig):
    original = rig.transcribe()

    def revoke(event):
        if event["action"] == "voice.review" and event["outcome"] == "success":
            rig.change(voice_revision=uuid4())

    rig.audit_hook = revoke
    fails(
        "stale",
        lambda: rig.service.review(
            rig.principal,
            original,
            TranscriptReviewAction(expected_revision=0, action="correct", corrected_text=CORRECTED),
        ),
    )
    assert original.transcript.reviews == ()
    assert rig.service._handles[rig.request.request_id] is original


def test_callbacks_finish_before_provider_and_audit_never_holds_service_lock(rig):
    active_callback = False

    def authorization(**kwargs):
        nonlocal active_callback
        active_callback = True
        try:
            return rig.authorize(**kwargs)
        finally:
            active_callback = False

    def check():
        assert not active_callback
        assert rig.service._lock.acquire(blocking=False)
        rig.service._lock.release()

    rig.service._authorize_callback = authorization
    rig.provider.hook = check
    rig.audit_hook = lambda event: check()
    result = rig.transcribe()
    assert result.transcript.state == "unverified"


def test_gnani_normalized_timeout_category_remains_sanitized(rig):
    class ProviderError(Exception):
        code = "timeout"

    def fail():
        raise ProviderError("PRIVATE_SECRET")

    rig.provider.hook = fail
    fails("provider_timeout", rig.transcribe)


def test_close_invalidates_existing_handles(rig):
    original = rig.transcribe()
    rig.service.close()
    fails(
        "disabled",
        lambda: rig.service.review(
            rig.principal,
            original,
            TranscriptReviewAction(expected_revision=0, action="correct", corrected_text=CORRECTED),
        ),
    )
    assert not rig.service._handles


def test_http_acceptance_boundary_rechecks_context_without_review_changes(rig):
    handle = rig.transcribe()
    assert rig.service.validate_current_transcript(rig.principal, handle) is None
    assert handle.transcript.state == "unverified" and handle.transcript.reviews == ()
    rig.change(care_revision=uuid4())
    fails("stale", lambda: rig.service.validate_current_transcript(rig.principal, handle))
    assert rig.events[-1]["outcome"] == "stale"


def test_http_acceptance_boundary_rejects_forged_handle_and_fails_closed_on_audit(rig):
    handle = rig.transcribe()
    fails("stale", lambda: rig.service.validate_current_transcript(rig.principal, replace(handle)))
    rig.change(recording_permitted=False)
    rig.service._audit_callback = None
    fails(
        "audit_unavailable", lambda: rig.service.validate_current_transcript(rig.principal, handle)
    )
