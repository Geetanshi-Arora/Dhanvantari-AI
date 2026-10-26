"""Offline mappings only. Fictional entries are NOT documented Gnani.ai support.

The eligible Hindi TTS tuple alone comes from supplied documentation; no test
creates production configuration, sends requests or verifies human consent.
"""

import socket
from uuid import UUID

import httpx
import pytest
from app.integrations.gnani import (
    GNANI_REVIEWED_CAPABILITIES,
    GnaniLanguageMapping,
    GnaniSTTAdapter,
    GnaniSTTConfig,
    GnaniSTTError,
    GnaniTTSAdapter,
    GnaniTTSConfig,
    GnaniTTSError,
    GnaniTTSMapping,
)
from app.integrations.voice_capabilities import VoiceCapabilityMapping, VoiceCapabilityRegistry
from app.schemas.voice import GNANI_CAPABILITIES, SpeechToTextRequest, TextToSpeechRequest
from pydantic import SecretStr, ValidationError

LANGUAGES = ("en", "hi", "bn", "ta", "te", "mr", "gu", "pa")
UID = UUID("10000000-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live networking prohibited")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def entry(operation="stt", language="hi", **changes):
    values = dict(
        operation=operation,
        language=language,
        provider_language="fictional-" + language,
        media_types=frozenset({"audio/wav"}),
        documentation_reference="FICTIONAL-TEST-ONLY",
    )
    if operation == "tts":
        values.update(voice="FictionalVoice", model="fictional-model")
    return VoiceCapabilityMapping.model_validate(values | changes)


def identity(item):
    return dict(
        operation=item.operation,
        language=item.language,
        provider_language=item.provider_language,
        voice=item.voice,
        model=item.model,
    )


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("operation", ["stt", "tts"])
def test_all_application_codes_map_exactly_in_fictional_provider_registry(operation, language):
    item = entry(operation, language)
    registry = VoiceCapabilityRegistry(provider="fictional_offline", mappings=(item,))
    assert registry.eligible(**identity(item)) == item
    with pytest.raises(ValueError, match="disabled"):
        registry.require_enabled(media_type="audio/wav", **identity(item))
    assert registry.require_enabled(enabled=True, media_type="audio/wav", **identity(item)) == item


@pytest.mark.parametrize("enabled", [False, None, 1, "true", (), object()])
def test_gate_requires_explicit_boolean_true(enabled):
    item = entry()
    registry = VoiceCapabilityRegistry(provider="fictional_offline", mappings=(item,))
    with pytest.raises(ValueError, match="disabled"):
        registry.require_enabled(enabled=enabled, media_type="audio/wav", **identity(item))


@pytest.mark.parametrize("operation", ["stt", "tts"])
def test_one_operation_never_authorizes_the_other(operation):
    item = entry(operation)
    registry = VoiceCapabilityRegistry(provider="fictional_offline", mappings=(item,))
    opposite = entry("tts" if operation == "stt" else "stt")
    with pytest.raises(ValueError):
        registry.require_enabled(enabled=True, media_type="audio/wav", **identity(opposite))


@pytest.mark.parametrize(
    "changes",
    [
        {"language": "en"},
        {"language": "fr"},
        {"language": "hi-IN"},
        {"provider_language": "hi"},
        {"provider_language": "hi-IN"},
        {"provider_language": "unknown"},
        {"operation": "auto"},
        {"voice": "FictionalVoice"},
    ],
)
def test_no_language_identifier_or_operation_fallback(changes):
    item = entry()
    registry = VoiceCapabilityRegistry(provider="fictional_offline", mappings=(item,))
    with pytest.raises(ValueError):
        registry.eligible(**(identity(item) | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "auto"},
        {"language": "fr"},
        {"provider_language": ""},
        {"provider_language": "../PRIVATE"},
        {"documentation_reference": " "},
        {"documentation_reference": "PRIVATE\x00"},
        {"media_types": frozenset()},
        {"media_types": frozenset({"text/plain"})},
        {"voice": "FictionalVoice"},
    ],
)
def test_malformed_generic_mapping_is_rejected(changes):
    with pytest.raises(ValidationError) as error:
        entry(**changes)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("changes", [{"voice": None}, {"model": None}, {"model": "x" * 65}])
def test_tts_requires_explicit_bounded_voice_and_model(changes):
    with pytest.raises(ValidationError):
        entry("tts", **changes)


@pytest.mark.parametrize(
    "kind", ["duplicate", "conflicting_code", "collapsed_languages", "conflicting_voice"]
)
def test_duplicate_or_conflicting_registry_entries_are_rejected(kind):
    first = entry("tts") if kind == "conflicting_voice" else entry()
    second = first
    if kind == "conflicting_code":
        second = entry(provider_language="other-fixture")
    elif kind == "collapsed_languages":
        second = entry(language="en", provider_language=first.provider_language)
    elif kind == "conflicting_voice":
        second = entry("tts", voice="DifferentVoice")
    with pytest.raises(ValidationError, match="Duplicate or conflicting"):
        VoiceCapabilityRegistry(provider="fictional_offline", mappings=(first, second))


def test_gnani_documented_hindi_tts_is_eligible_but_not_enabled():
    registry = GNANI_REVIEWED_CAPABILITIES
    assert len(registry.mappings) == 1
    item = registry.eligible(
        operation="tts",
        language="hi",
        provider_language="hi-IN",
        voice="Nalini",
        model="timbre-v2.5",
    )
    assert item.media_types == frozenset({"audio/wav"})
    with pytest.raises(ValueError, match="disabled"):
        registry.require_enabled(media_type="audio/wav", **identity(item))
    assert not GNANI_CAPABILITIES.stt.available and not GNANI_CAPABILITIES.tts.available
    assert not GnaniSTTAdapter(GnaniSTTConfig()).capabilities.stt.available
    configured = GnaniTTSConfig(
        mappings=(
            GnaniTTSMapping(
                language="hi", provider_language="hi-IN", voice="Nalini", model="timbre-v2.5"
            ),
        ),
        documentation_reference="SUPPLIED-TTS-DOCUMENTATION",
    )
    assert configured.enabled is False
    assert not GnaniTTSAdapter(configured).capabilities.tts.available


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_gnani_stt_language_identifier_is_assumed_documented(language):
    with pytest.raises(ValidationError):
        GnaniSTTConfig(
            language_mappings=(GnaniLanguageMapping(language=language, language_code=language),),
            language_documentation_reference="UNVERIFIED-REFERENCE",
        )
    with pytest.raises(ValueError):
        GNANI_REVIEWED_CAPABILITIES.eligible(
            operation="stt", language=language, provider_language="hi-IN"
        )


def test_missing_mapping_even_with_enablement_cannot_construct_live_configuration():
    with pytest.raises(ValidationError):
        GnaniSTTConfig(enabled=True)
    with pytest.raises(ValidationError):
        GnaniTTSConfig(enabled=True)
    with pytest.raises(ValueError):
        VoiceCapabilityRegistry(provider="fictional_offline").require_enabled(
            enabled=True,
            operation="stt",
            language="hi",
            provider_language="hi",
            media_type="audio/wav",
        )


def test_unsupported_voice_model_language_or_codec_cannot_be_selected():
    item = GNANI_REVIEWED_CAPABILITIES.mappings[0]
    for changes in (
        {"voice": "OtherVoice"},
        {"model": "other-model"},
        {"language": "en"},
        {"provider_language": "en-US"},
    ):
        with pytest.raises(ValueError):
            GNANI_REVIEWED_CAPABILITIES.require_enabled(
                enabled=True, media_type="audio/wav", **(identity(item) | changes)
            )
    with pytest.raises(ValueError):
        GNANI_REVIEWED_CAPABILITIES.require_enabled(
            enabled=True, media_type="audio/x-unverified", **identity(item)
        )


def test_registry_and_adapter_flags_are_immutable_and_strict():
    with pytest.raises(ValidationError):
        GNANI_REVIEWED_CAPABILITIES.mappings = ()
    with pytest.raises(ValidationError):
        GNANI_REVIEWED_CAPABILITIES.mappings[0].provider_language = "other"
    for model in (GnaniSTTConfig, GnaniTTSConfig):
        with pytest.raises(ValidationError):
            model(enabled="true")
    broken = GNANI_REVIEWED_CAPABILITIES.model_copy(
        update={"mappings": GNANI_REVIEWED_CAPABILITIES.mappings * 2}
    )
    with pytest.raises(ValidationError):
        GnaniTTSConfig(reviewed_capabilities=broken)


def test_disabled_adapters_never_construct_an_http_request_even_with_credentials_and_mappings():
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500))
    stt_item = entry()
    registry = VoiceCapabilityRegistry(provider="gnani", mappings=(stt_item,))
    cfg = GnaniSTTConfig(
        api_key_id=SecretStr("FICTIONAL-NOT-A-KEY"),
        language_mappings=(
            GnaniLanguageMapping(language="hi", language_code=stt_item.provider_language),
        ),
        reviewed_capabilities=registry,
        language_documentation_reference="FICTIONAL-ONLY",
    )
    adapter = GnaniSTTAdapter(cfg, transport=transport)
    assert not adapter.capabilities.stt.available
    request = SpeechToTextRequest.model_construct(
        request_id=UID,
        patient_id=UID,
        visit_id=UID,
        language="hi",
        media_type="audio/wav",
        byte_count=1,
        duration_ms=1,
    )
    with pytest.raises(GnaniSTTError, match="capability_disabled"):
        adapter.transcribe(audio=b"FICTIONAL", filename="fixture.wav", request=request)
    tts_item = GNANI_REVIEWED_CAPABILITIES.mappings[0]
    tts_cfg = GnaniTTSConfig(
        api_key_id=SecretStr("FICTIONAL-NOT-A-KEY"),
        mappings=(
            GnaniTTSMapping(
                language=tts_item.language,
                provider_language=tts_item.provider_language,
                voice=tts_item.voice,
                model=tts_item.model,
            ),
        ),
        documentation_reference="SUPPLIED-DOCS",
    )
    tts_adapter = GnaniTTSAdapter(tts_cfg, transport=transport)
    assert not tts_adapter.capabilities.tts.available
    tts_request = TextToSpeechRequest.model_construct(
        request_id=UID,
        patient_id=UID,
        visit_id=UID,
        language="hi",
        media_type="audio/wav",
        text="FICTIONAL-ONLY",
    )
    with pytest.raises(GnaniTTSError, match="capability_disabled"):
        tts_adapter.synthesize(request=tts_request)
    assert calls == []


def test_wrong_provider_registry_is_not_a_gnani_trust_source():
    wrong = VoiceCapabilityRegistry(provider="fictional_other")
    for model in (GnaniSTTConfig, GnaniTTSConfig):
        with pytest.raises(ValidationError):
            model(reviewed_capabilities=wrong)
