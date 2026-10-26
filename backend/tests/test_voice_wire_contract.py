"""Shared fictional wire fixture plus real routes/service/Gnani with fake HTTP.

No server, actual DB, provider traffic or approval records. In-memory silent PCM
and a FICTIONAL STT registry are test inputs, not verified Gnani language support.
"""

import base64
import json
import socket
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_patient_routes import headers
from test_patient_routes import web as web

from app.integrations import gnani
from app.integrations.voice_capabilities import VoiceCapabilityMapping, VoiceCapabilityRegistry
from app.routes.voice import (
    ReviewBody,
    SynthesisBody,
    TranscriptionBody,
    VoiceRouteBindings,
    VoiceWorkflows,
)
from app.schemas.voice import ReviewedTranscript, TranscriptDraft, VoiceCapabilities
from app.services.voice import VoiceAuthorization, VoiceService, VoiceServiceError

DATA = json.loads(
    (Path(__file__).resolve().parents[2] / "tests/fixtures/voice-wire-contract.json").read_text()
)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def bridge(web, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network forbidden in shared contract tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    # TestClient uses an in-process ASGI transport; actual HTTPTransport is blocked.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    ids = iter(UUID(value) for value in DATA["http_request_ids"])
    monkeypatch.setattr("app.main.uuid4", lambda: next(ids))

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(gnani, "datetime", FixedClock)
    calls, events, handles = [], [], {}
    flags = {"care_granted": True}
    hook = {"during_provider": lambda: None}

    def respond(request):
        calls.append(request.url.path)
        assert request.url.host == "api.vachana.ai"
        assert request.headers["X-API-Key-ID"] == "FICTIONAL-CONTRACT-KEY"
        hook["during_provider"]()
        if str(request.url) == gnani.ENDPOINT:
            assert b'name="format"\r\n\r\nverbatim' in request.content
            assert b"FICTIONAL_STT_HI" in request.content
            assert (
                base64.b64decode(DATA["transcription"]["request"]["audio_base64"])
                in request.content
            )
            return httpx.Response(
                200,
                json=dict(
                    success=True,
                    request_id="FICTIONAL-PROVIDER-ID",
                    timestamp="FICTIONAL-PROVIDER-TIME",
                    transcript=DATA["transcription"]["response"]["original_text"],
                ),
            )
        assert str(request.url) == gnani.TTS_ENDPOINT
        body = json.loads(request.content)
        assert body["text"] == DATA["synthesis"]["request"]["text"]
        assert (body["language"], body["voice"], body["model"]) == (
            "hi-IN",
            "Nalini",
            "timbre-v2.5",
        )
        return httpx.Response(
            200,
            content=base64.b64decode(DATA["synthesis"]["audio_base64"]),
            headers={"Content-Type": "audio/wav"},
        )

    registry = VoiceCapabilityRegistry(
        provider="gnani",
        mappings=(
            VoiceCapabilityMapping(
                operation="stt",
                language="hi",
                provider_language="FICTIONAL_STT_HI",
                media_types=frozenset({"audio/wav"}),
                documentation_reference="FICTIONAL TEST REGISTRY ONLY; NOT VERIFIED DOCUMENTATION",
            ),
            *gnani.GNANI_REVIEWED_CAPABILITIES.mappings,
        ),
    )
    stt = gnani.GnaniSTTAdapter(
        gnani.GnaniSTTConfig(
            api_key_id=SecretStr("FICTIONAL-CONTRACT-KEY"),
            enabled=True,
            reviewed_capabilities=registry,
            language_mappings=(
                gnani.GnaniLanguageMapping(language="hi", language_code="FICTIONAL_STT_HI"),
            ),
            language_documentation_reference="FICTIONAL TEST ONLY",
        ),
        transport=httpx.MockTransport(respond),
    )
    tts = gnani.GnaniTTSAdapter(
        gnani.GnaniTTSConfig(
            api_key_id=SecretStr("FICTIONAL-CONTRACT-KEY"),
            enabled=True,
            mappings=(
                gnani.GnaniTTSMapping(
                    language="hi", provider_language="hi-IN", voice="Nalini", model="timbre-v2.5"
                ),
            ),
            documentation_reference="TEST ONLY: supplied Hindi example, not production enablement",
        ),
        transport=httpx.MockTransport(respond),
    )
    capabilities = VoiceCapabilities(
        provider="gnani", stt=stt.capabilities.stt, tts=tts.capabilities.tts
    )
    epochs = dict(
        context_revision=uuid4(),
        assignment_revision=uuid4(),
        care_revision=uuid4(),
        voice_revision=uuid4(),
    )

    def authorize(*, principal, context, operation, text_digest):
        assert web.provider.user.called  # Existing verified Auth boundary, no DB.
        return VoiceAuthorization(
            **context.model_dump(),
            **epochs,
            actor_id=principal.id,
            session_hash=principal.session_hash,
            authenticated=True,
            active_nurse=True,
            patient_assigned=str(context.patient_id) == DATA["context"]["patient_id"]
            and str(context.visit_id) == DATA["context"]["visit_id"],
            fictional_patient=True,
            care_granted=flags["care_granted"],
            voice_notice_approved=True,
            recording_permitted=True,
            external_voice_processing_permitted=True,
            synthesis_permitted=True,
            tts_reviewed_by=principal.id,
            tts_text_digest=sha256(DATA["synthesis"]["request"]["text"].encode()).hexdigest(),
        )

    def remember(handle):
        assert not handles
        handles[handle.transcript.draft.request_id] = handle

    def replace(old, new):
        key = old.transcript.draft.request_id
        if handles.get(key) is not old:
            raise VoiceServiceError("stale")
        handles[key] = new

    service = VoiceService(
        authorizer=authorize,
        auditor=lambda **event: events.append(event),
        stt=stt,
        tts=tts,
        capabilities=capabilities,
        enabled=True,
        clock=lambda: NOW,
    )
    web.app.state.voice_bindings = VoiceRouteBindings(
        enabled=True,
        single_process=True,
        service=service,
        capabilities=capabilities,
        workflows=VoiceWorkflows(remember, handles.get, replace),
    )
    try:
        yield web, calls, flags, hook, events
    finally:
        service.close()
        handles.clear()


def test_shared_request_and_response_schemas():
    assert set(TranscriptionBody.model_fields) == set(DATA["transcription"]["request"])
    assert set(SynthesisBody.model_fields) == set(DATA["synthesis"]["request"])
    assert set(ReviewBody.model_fields) == set(DATA["correction"]["request"])
    assert TranscriptionBody.model_validate(DATA["transcription"]["request"]).byte_count == 76
    SynthesisBody.model_validate(DATA["synthesis"]["request"])
    ReviewBody.model_validate(DATA["correction"]["request"])
    ReviewBody.model_validate(DATA["confirmation"]["request"])
    assert (
        TranscriptDraft.model_validate(DATA["transcription"]["response"]).model_dump(mode="json")
        == DATA["transcription"]["response"]
    )
    for name in ("correction", "confirmation"):
        assert (
            ReviewedTranscript.model_validate(DATA[name]["response"]).model_dump(mode="json")
            == DATA[name]["response"]
        )


def test_real_routes_service_and_gnani_match_shared_wire_fixture(bridge):
    web, calls, _, _, events = bridge
    for name in ("transcription", "correction", "confirmation", "synthesis"):
        fixture = DATA[name]
        response = web.client.post(fixture["path"], json=fixture["request"], headers=headers())
        assert response.status_code == 200
        if name == "synthesis":
            assert response.content == base64.b64decode(fixture["audio_base64"])
            for key, value in fixture["headers"].items():
                assert response.headers[key] == value
        else:
            assert response.json() == fixture["response"]
    assert calls == ["/stt/v3", "/api/v1/tts/inference"]
    assert all(
        set(event) == {"action", "outcome", "request_id", "actor_id", "resource_id"}
        for event in events
    )
    assert DATA["transcription"]["response"]["original_text"] not in repr(events)


def test_real_gnani_result_discarded_after_inflight_consent_revocation(bridge):
    web, calls, flags, hook, _ = bridge
    hook["during_provider"] = lambda: flags.update(care_granted=False)
    response = web.client.post(
        DATA["transcription"]["path"], json=DATA["transcription"]["request"], headers=headers()
    )
    assert response.status_code == 409 and len(calls) == 1
    assert DATA["transcription"]["response"]["original_text"] not in response.text
