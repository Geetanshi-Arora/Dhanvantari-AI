"""Offline fictional metadata/text only. No audio, provider support claim or consent.

Capabilities below belong to a fictional test provider, NOT Gnani.ai. Synthetic
Principal objects exercise trusted-server contracts, not actual authentication.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from app.core.security import Principal
from app.schemas.voice import (
    GNANI_CAPABILITIES,
    PlaybackMetadata,
    ReviewedTranscript,
    SpeechCapability,
    SpeechToTextRequest,
    TextToSpeechRequest,
    TranscriptDraft,
    TranscriptReviewAction,
    VoiceCapabilities,
    review_transcript,
    validate_playback_metadata,
    validate_transcript_result,
)
from pydantic import ValidationError

NOW = datetime(2026, 1, 2, tzinfo=UTC)
UID = UUID("10000000-0000-4000-8000-000000000001")
MEDIA = "audio/x-fictional-test"
TEXT = "FICTIONAL TRANSCRIPT FIXTURE ONLY.\n"
CORRECTED = "FICTIONAL NURSE CORRECTION FIXTURE ONLY.\n"
LANGUAGES = ("en", "hi", "bn", "ta", "te", "mr", "gu", "pa")
NURSE = Principal(UID, "Fictional Nurse", "nurse", "FICTIONAL-NOT-A-SESSION")
SUPPORT = SpeechCapability(
    available=True, languages=frozenset(LANGUAGES), media_types=frozenset({MEDIA})
)
CAPS = VoiceCapabilities(provider="fictional_offline", stt=SUPPORT, tts=SUPPORT)
CONTEXT = {"voice_capabilities": CAPS}


def identity(**changes):
    return dict(request_id=UID, patient_id=UID, visit_id=UID, language="hi") | changes


def stt(**changes):
    return identity() | dict(media_type=MEDIA, byte_count=1024, duration_ms=1000) | changes


def tts(**changes):
    return identity() | dict(text=TEXT, media_type=MEDIA) | changes


def draft(**changes):
    return TranscriptDraft.model_validate(
        identity() | dict(provider=CAPS.provider, original_text=TEXT, created_at=NOW) | changes
    )


def playback(**changes):
    return PlaybackMetadata.model_validate(
        identity()
        | dict(provider=CAPS.provider, media_type=MEDIA, byte_count=1024, duration_ms=1000)
        | changes
    )


def correct(current, **changes):
    action = TranscriptReviewAction(
        expected_revision=len(current.reviews), action="correct", corrected_text=CORRECTED
    )
    return review_transcript(
        current, action, principal=NURSE, at=NOW + timedelta(seconds=1), **changes
    )


def confirm(current, *, principal=NURSE, at=None):
    action = TranscriptReviewAction(expected_revision=len(current.reviews), action="confirm")
    return review_transcript(
        current, action, principal=principal, at=at or NOW + timedelta(seconds=2)
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_eight_app_codes_are_valid_only_with_explicit_fictional_capabilities(language):
    request = SpeechToTextRequest.model_validate(stt(language=language), context=CONTEXT)
    assert request.language == language
    spoken = TextToSpeechRequest.model_validate(tts(language=language), context=CONTEXT)
    assert spoken.language == language


@pytest.mark.parametrize("language", ["fr", "EN", "hi-IN", "auto", "", None, 123])
@pytest.mark.parametrize("model,payload", [(SpeechToTextRequest, stt), (TextToSpeechRequest, tts)])
def test_invalid_or_unsupported_app_languages_are_never_substituted(model, payload, language):
    with pytest.raises(ValidationError):
        model.model_validate(payload(language=language), context=CONTEXT)


@pytest.mark.parametrize(
    "context", [None, {}, {"voice_capabilities": {}}, {"voice_capabilities": GNANI_CAPABILITIES}]
)
@pytest.mark.parametrize("model,payload", [(SpeechToTextRequest, stt), (TextToSpeechRequest, tts)])
def test_missing_or_default_unavailable_capabilities_fail_closed(model, payload, context):
    with pytest.raises(ValidationError):
        model.model_validate(payload(), context=context)
    assert not GNANI_CAPABILITIES.stt.available and not GNANI_CAPABILITIES.tts.available
    assert not GNANI_CAPABILITIES.stt.languages and not GNANI_CAPABILITIES.tts.languages
    assert not GNANI_CAPABILITIES.stt.media_types and not GNANI_CAPABILITIES.tts.media_types


@pytest.mark.parametrize("operation", ["stt", "tts"])
def test_stt_and_tts_support_are_independent(operation):
    capabilities = VoiceCapabilities(provider="fictional_offline", **{operation: SUPPORT})
    context = {"voice_capabilities": capabilities}
    accepted = SpeechToTextRequest if operation == "stt" else TextToSpeechRequest
    rejected = TextToSpeechRequest if operation == "stt" else SpeechToTextRequest
    good, bad = (stt, tts) if operation == "stt" else (tts, stt)
    assert accepted.model_validate(good(), context=context)
    with pytest.raises(ValidationError):
        rejected.model_validate(bad(), context=context)


@pytest.mark.parametrize("operation", ["stt", "tts"])
def test_no_language_or_format_fallback(operation):
    support = SpeechCapability(
        available=True, languages=frozenset({"en"}), media_types=frozenset({MEDIA})
    )
    context = {
        "voice_capabilities": VoiceCapabilities(
            provider="fictional_offline", **{operation: support}
        )
    }
    model, payload = (
        (SpeechToTextRequest, stt) if operation == "stt" else (TextToSpeechRequest, tts)
    )
    with pytest.raises(ValidationError):
        model.model_validate(payload(language="hi"), context=context)
    with pytest.raises(ValidationError):
        model.model_validate(
            payload(language="en", media_type="audio/x-other-fictional"), context=context
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"available": True},
        {"available": "true"},
        {"languages": {"en"}},
        {"media_types": {MEDIA}},
        {"available": True, "languages": {"en"}},
        {"available": True, "languages": {"fr"}, "media_types": {MEDIA}},
        {"available": True, "languages": {"en"}, "media_types": {"PRIVATE_URL"}},
    ],
)
def test_invalid_capability_declarations_rejected(changes):
    with pytest.raises(ValidationError):
        SpeechCapability.model_validate(changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("byte_count", 0),
        ("byte_count", 4 * 1024 * 1024 + 1),
        ("byte_count", True),
        ("byte_count", "1024"),
        ("duration_ms", 0),
        ("duration_ms", 60001),
        ("duration_ms", 1.1),
        ("duration_ms", False),
        ("media_type", "text/plain"),
        ("media_type", "audio/" + "x" * 51),
        ("request_id", "not-a-uuid"),
        ("patient_id", UID.bytes),
        ("visit_id", 1),
        ("patient_id", UID.hex),
        ("audio", "PRIVATE_AUDIO"),
        ("nurse_id", UID),
        ("consent", True),
        ("endpoint", "PRIVATE_ENDPOINT"),
    ],
)
def test_invalid_or_oversized_stt_metadata(field, value):
    with pytest.raises(ValidationError):
        SpeechToTextRequest.model_validate(stt(**{field: value}), context=CONTEXT)


@pytest.mark.parametrize("count,duration", [(1, 1), (4 * 1024 * 1024, 60000)])
def test_stt_application_boundaries(count, duration):
    assert SpeechToTextRequest.model_validate(
        stt(byte_count=count, duration_ms=duration), context=CONTEXT
    )


@pytest.mark.parametrize(
    "field",
    ["request_id", "patient_id", "visit_id", "language", "media_type", "byte_count", "duration_ms"],
)
def test_stt_missing_required_fields(field):
    payload = stt()
    del payload[field]
    with pytest.raises(ValidationError):
        SpeechToTextRequest.model_validate(payload, context=CONTEXT)


@pytest.mark.parametrize(
    "field", ["request_id", "patient_id", "visit_id", "language", "media_type", "text"]
)
def test_tts_missing_required_fields(field):
    payload = tts()
    del payload[field]
    with pytest.raises(ValidationError):
        TextToSpeechRequest.model_validate(payload, context=CONTEXT)


@pytest.mark.parametrize("value", ["", " \n", "x" * 2001, 123, b"PRIVATE", "PRIVATE\x00", "\ud800"])
def test_tts_text_validation_does_not_expose_content(value):
    with pytest.raises(ValidationError) as error:
        TextToSpeechRequest.model_validate(tts(text=value), context=CONTEXT)
    assert "PRIVATE" not in str(error.value)


def test_draft_is_unverified_and_preserves_exact_original_language_text_and_provenance():
    value = draft()
    assert value.state == "unverified" and value.language == "hi"
    assert (
        value.original_text == TEXT and value.provider == CAPS.provider and value.request_id == UID
    )
    assert TEXT not in repr(value)
    for changes in (
        {"state": "nurse_confirmed"},
        {"approved": True},
        {"translated_text": "PRIVATE"},
        {"reviews": []},
    ):
        with pytest.raises(ValidationError):
            draft(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"original_text": ""},
        {"original_text": "x" * 8001},
        {"original_text": "PRIVATE\x00"},
        {"created_at": NOW.replace(tzinfo=None)},
        {"language": "fr"},
        {"provider": "PRIVATE/endpoint"},
    ],
)
def test_malformed_normalized_transcript_responses(changes):
    with pytest.raises(ValidationError) as error:
        draft(**changes)
    assert "PRIVATE" not in str(error.value)


def test_transcript_review_requires_explicit_nurse_action_and_keeps_history():
    initial = ReviewedTranscript(draft=draft())
    corrected = correct(initial)
    confirmed = confirm(corrected)
    assert initial.state == "unverified" and initial.current_text == TEXT
    assert corrected.state == "nurse_corrected" and corrected.current_text == CORRECTED
    assert confirmed.state == "nurse_confirmed" and confirmed.current_text == CORRECTED
    assert confirmed.draft.original_text == TEXT and confirmed.draft.language == "hi"
    assert confirmed.reviews[:1] == corrected.reviews
    assert all(event.nurse_id == NURSE.id for event in confirmed.reviews)
    assert confirmed.draft.state == "unverified"  # Recognition never becomes approval.
    assert ReviewedTranscript.model_validate_json(confirmed.model_dump_json()) == confirmed
    assert TEXT not in repr(confirmed) and CORRECTED not in repr(confirmed)
    with pytest.raises(ValueError, match="review conflict"):
        correct(confirmed)


def test_explicit_confirmation_without_editing_original():
    confirmed = confirm(ReviewedTranscript(draft=draft()))
    assert confirmed.state == "nurse_confirmed" and confirmed.current_text == TEXT


@pytest.mark.parametrize(
    "principal",
    [
        None,
        "nurse",
        Principal(UID, "Fictional", "doctor", "FICTIONAL"),
        Principal("not-uuid", "Fictional", "nurse", "FICTIONAL"),
    ],
)
def test_only_server_supplied_nurse_principal_can_transition(principal):
    with pytest.raises(ValueError, match="Verified nurse principal required"):
        confirm(ReviewedTranscript(draft=draft()), principal=principal)


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_revision": 0, "action": "correct"},
        {"expected_revision": 0, "action": "confirm", "corrected_text": "PRIVATE"},
        {"expected_revision": True, "action": "confirm"},
        {"expected_revision": 0, "action": "approve_prescription"},
        {"expected_revision": 0, "action": "confirm", "nurse_id": UID},
    ],
)
def test_malformed_review_actions_are_rejected(payload):
    with pytest.raises(ValidationError) as error:
        TranscriptReviewAction.model_validate(payload)
    assert "PRIVATE" not in str(error.value)


def test_stale_revision_backdated_or_naive_review_times_are_rejected():
    current = correct(ReviewedTranscript(draft=draft()))
    with pytest.raises(ValueError, match="review conflict"):
        review_transcript(
            current,
            TranscriptReviewAction(expected_revision=0, action="confirm"),
            principal=NURSE,
            at=NOW,
        )
    for at in (NOW - timedelta(seconds=1), NOW.replace(tzinfo=None)):
        with pytest.raises(ValueError):
            confirm(current, at=at)


def test_inconsistent_or_forged_review_history_rejected():
    value = ReviewedTranscript(draft=draft())
    with pytest.raises(ValidationError):
        ReviewedTranscript(draft=draft(), state="nurse_confirmed")
    corrected = correct(value)
    bad_event = corrected.reviews[0].model_copy(update={"revision": 2})
    with pytest.raises(ValidationError):
        ReviewedTranscript(draft=draft(), reviews=(bad_event,), state="nurse_corrected")
    with pytest.raises(ValidationError):
        ReviewedTranscript.model_validate(value.model_copy(update={"state": "nurse_confirmed"}))


@pytest.mark.parametrize("field", ["language", "request_id", "patient_id", "visit_id", "provider"])
def test_transcript_response_must_match_bound_request_without_translation(field):
    request = SpeechToTextRequest.model_validate(stt(), context=CONTEXT)
    different = (
        "en"
        if field == "language"
        else "other_provider"
        if field == "provider"
        else UUID("20000000-0000-4000-8000-000000000002")
    )
    with pytest.raises(ValueError, match="Transcript request mismatch"):
        validate_transcript_result(draft(**{field: different}), request=request, capabilities=CAPS)
    assert (
        validate_transcript_result(draft(), request=request, capabilities=CAPS).original_text
        == TEXT
    )


@pytest.mark.parametrize(
    "field", ["language", "request_id", "patient_id", "visit_id", "provider", "media_type"]
)
def test_playback_metadata_must_match_bound_tts_request(field):
    request = TextToSpeechRequest.model_validate(tts(), context=CONTEXT)
    different = {
        "language": "en",
        "provider": "other_provider",
        "media_type": "audio/x-other-fictional",
    }.get(field, UUID("20000000-0000-4000-8000-000000000002"))
    with pytest.raises(ValueError, match="Playback request mismatch"):
        validate_playback_metadata(
            playback(**{field: different}), request=request, capabilities=CAPS
        )
    assert (
        validate_playback_metadata(playback(), request=request, capabilities=CAPS).language == "hi"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"audio_url": "PRIVATE"},
        {"audio": b"PRIVATE"},
        {"autoplay": True},
        {"byte_count": 0},
        {"duration_ms": 60001},
    ],
)
def test_playback_contains_no_storage_or_automatic_playback_instruction(changes):
    with pytest.raises(ValidationError) as error:
        playback(**changes)
    assert "PRIVATE" not in str(error.value)


def test_json_request_validation_matches_python_and_stays_fail_closed():
    for model, payload in ((SpeechToTextRequest, stt()), (TextToSpeechRequest, tts())):
        raw = json.dumps(payload, default=str)
        assert model.model_validate_json(raw, context=CONTEXT) == model.model_validate(
            payload, context=CONTEXT
        )
        with pytest.raises(ValidationError):
            model.model_validate_json(raw)
        with pytest.raises(ValidationError):
            model.model_validate_json(raw, context={"voice_capabilities": GNANI_CAPABILITIES})


def test_models_are_immutable_and_forged_capabilities_are_revalidated():
    with pytest.raises(ValidationError):
        GNANI_CAPABILITIES.stt = SUPPORT
    with pytest.raises(ValidationError):
        SUPPORT.available = False
    bad = VoiceCapabilities.model_construct(
        provider="fictional_offline",
        stt=SUPPORT.model_copy(update={"available": False}),
        tts=SUPPORT,
    )
    with pytest.raises(ValidationError):
        SpeechToTextRequest.model_validate(stt(), context={"voice_capabilities": bad})
    request = SpeechToTextRequest.model_validate(stt(), context=CONTEXT)
    with pytest.raises(ValueError):
        validate_transcript_result(
            draft(), request=request.model_copy(update={"language": "fr"}), capabilities=CAPS
        )
    with pytest.raises(ValidationError):
        draft().original_text = "PRIVATE"


@pytest.mark.parametrize(
    "field",
    ["request_id", "patient_id", "visit_id", "language", "provider", "original_text", "created_at"],
)
def test_missing_required_transcript_fields(field):
    value = draft().model_dump(mode="json")
    del value[field]
    with pytest.raises(ValidationError):
        TranscriptDraft.model_validate(value)


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "patient_id",
        "visit_id",
        "language",
        "provider",
        "media_type",
        "byte_count",
        "duration_ms",
    ],
)
def test_missing_required_playback_fields(field):
    value = playback().model_dump(mode="json")
    del value[field]
    with pytest.raises(ValidationError):
        PlaybackMetadata.model_validate(value)


@pytest.mark.parametrize(
    "value", [True, 123, 1.1, "123", b"PRIVATE_TIMESTAMP", "PRIVATE_TIMESTAMP"]
)
def test_timestamp_coercion_and_malformed_values_are_rejected(value):
    with pytest.raises(ValidationError) as error:
        draft(created_at=value)
    assert "PRIVATE_TIMESTAMP" not in str(error.value)


def test_review_history_is_bounded_and_cannot_be_extended_silently():
    current = ReviewedTranscript(draft=draft())
    for _ in range(10):
        current = correct(current)
    assert len(current.reviews) == 10 and current.state == "nurse_corrected"
    with pytest.raises(ValidationError):
        correct(current)
    assert len(current.reviews) == 10


def test_capability_format_declarations_are_bounded_without_claiming_real_codecs():
    with pytest.raises(ValidationError):
        SpeechCapability(
            available=True,
            languages=frozenset({"en"}),
            media_types=frozenset("audio/x-fixture-" + str(index) for index in range(33)),
        )


def test_review_identity_metadata_cannot_be_submitted_as_authorization():
    with pytest.raises(ValidationError):
        ReviewedTranscript(draft=draft(), clinical_approved=True)
    with pytest.raises(ValidationError):
        TextToSpeechRequest.model_validate(tts(voice_recording_consent=True), context=CONTEXT)


def test_normalized_responses_round_trip_without_assuming_translation():
    value = draft()
    assert TranscriptDraft.model_validate_json(value.model_dump_json()) == value
    spoken = playback()
    assert PlaybackMetadata.model_validate_json(spoken.model_dump_json()) == spoken
    assert not hasattr(value, "translated_text")


def test_provider_free_contract_module_has_no_network_or_storage_execution():
    import ast
    from pathlib import Path

    source = Path(__file__).parents[1] / "app/schemas/voice.py"
    tree = ast.parse(source.read_text())
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert not any(
        name.startswith(
            ("httpx", "requests", "socket", "subprocess", "sqlalchemy", "app.main", "app.services")
        )
        for name in modules
    )
