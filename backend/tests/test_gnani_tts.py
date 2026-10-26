"""Offline TTS tests only: fictional text/key and generated in-memory silence.

The Hindi profile comes from the supplied documentation; enabling it here is NOT
production configuration, clinical approval, recording consent or a real API call.
"""

import io
import json
import logging
import socket
import struct
import wave
from dataclasses import FrozenInstanceError
from uuid import UUID

import httpx
import pytest
from app.integrations.gnani import (
    MAX_AUDIO_BYTES,
    TTS_ENDPOINT,
    GnaniTTSAdapter,
    GnaniTTSAudioConfig,
    GnaniTTSConfig,
    GnaniTTSError,
    GnaniTTSMapping,
)
from app.schemas.voice import GNANI_CAPABILITIES, TextToSpeechRequest
from pydantic import SecretStr, ValidationError

UID = UUID("10000000-0000-4000-8000-000000000001")
KEY = "FICTIONAL-TTS-KEY-NOT-A-CREDENTIAL"
TEXT = "FICTIONAL-SYNTHESIS-TEXT-NOT-PATIENT-DATA\n"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network forbidden")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def wav(*, rate=48000, channels=1, width=2, seconds=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00" * (rate * channels * width * seconds))
    return buffer.getvalue()


def mapping(**changes):
    return GnaniTTSMapping.model_validate(
        dict(language="hi", provider_language="hi-IN", voice="Nalini", model="timbre-v2.5")
        | changes
    )


def config(**changes):
    return GnaniTTSConfig.model_validate(
        dict(
            api_key_id=SecretStr(KEY),
            enabled=True,
            mappings=(mapping(),),
            documentation_reference="SUPPLIED-DOCUMENTATION-MOCK-ONLY",
        )
        | changes
    )


def request_for(adapter, **changes):
    return TextToSpeechRequest.model_validate(
        dict(
            request_id=UID,
            patient_id=UID,
            visit_id=UID,
            language="hi",
            media_type="audio/wav",
            text=TEXT,
        )
        | changes,
        context={"voice_capabilities": adapter.capabilities},
    )


def setup(reply=None):
    calls = []

    def handle(request):
        calls.append(request)
        if callable(reply):
            return reply(request)
        return (
            reply
            if reply is not None
            else httpx.Response(200, content=wav(), headers={"content-type": "audio/wav"})
        )

    adapter = GnaniTTSAdapter(config(), transport=httpx.MockTransport(handle))
    return adapter, request_for(adapter), calls


def safe(error):
    assert isinstance(error, GnaniTTSError)
    assert KEY not in str(error) and TEXT.strip() not in str(error)
    assert "https://" not in str(error) and "PRIVATE" not in str(error)


def test_documented_endpoint_json_key_and_verified_in_memory_result(caplog):
    caplog.set_level(logging.DEBUG)
    adapter, request, calls = setup()
    result = adapter.synthesize(request=request)
    assert len(calls) == 1
    sent = calls[0]
    assert sent.method == "POST" and str(sent.url) == TTS_ENDPOINT
    assert TTS_ENDPOINT == "https://api.vachana.ai/api/v1/tts/inference"
    assert not adapter.capabilities.stt.available
    assert sent.headers["X-API-Key-ID"] == KEY
    assert sent.headers["content-type"] == "application/json"
    assert json.loads(sent.content) == dict(
        text=TEXT,
        voice="Nalini",
        model="timbre-v2.5",
        language="hi-IN",
        speed=1.0,
        audio_config=dict(
            sample_rate=48000,
            num_channels=1,
            sample_width=2,
            encoding="linear_pcm",
            container="wav",
        ),
    )
    assert str(UID) not in sent.content.decode()
    assert sent.extensions["timeout"] == {"connect": 5, "read": 10, "write": 10, "pool": 5}
    assert result.audio == wav()
    assert result.metadata.byte_count == len(result.audio) and result.metadata.duration_ms == 1000
    assert result.metadata.language == "hi" and result.metadata.provider == "gnani"
    assert result.metadata.request_id == UID and result.metadata.media_type == "audio/wav"
    assert not result.playback_authorized
    assert TEXT.strip() not in caplog.text and KEY not in caplog.text
    assert KEY not in repr(adapter._config)
    assert "audio=" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.audio = b"PRIVATE"


def test_defaults_have_no_capabilities_and_missing_credentials_never_send():
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    adapter = GnaniTTSAdapter(GnaniTTSConfig(), transport=transport)
    assert not adapter.capabilities.stt.available and not adapter.capabilities.tts.available
    assert adapter.capabilities.tts.languages == frozenset()
    assert not GNANI_CAPABILITIES.tts.available and not GNANI_CAPABILITIES.stt.available
    with pytest.raises(GnaniTTSError, match="configuration_unavailable"):
        adapter.synthesize(request=None)
    configured = GnaniTTSAdapter(config(), transport=transport)
    request = request_for(configured)
    disabled = GnaniTTSAdapter(GnaniTTSConfig(api_key_id=SecretStr(KEY)), transport=transport)
    with pytest.raises(GnaniTTSError, match="capability_disabled"):
        disabled.synthesize(request=request)
    assert calls == []


@pytest.mark.parametrize(
    "language", ["en", "bn", "ta", "te", "mr", "gu", "pa", "fr", "auto", "hi-IN"]
)
def test_no_language_fallback(language):
    adapter, request, calls = setup()
    with pytest.raises(GnaniTTSError):
        adapter.synthesize(request=request.model_copy(update={"language": language}))
    assert calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"voice": "UnknownVoice"},
        {"model": "unknown-model"},
        {"provider_language": "en-US"},
        {"language": "en"},
        {"voice": "Nalini/PRIVATE"},
        {"model": "x" * 65},
    ],
)
def test_unverified_profiles_and_unsupported_voices_are_rejected(changes):
    with pytest.raises(ValidationError) as error:
        mapping(**changes)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_rate": 8000},
        {"sample_rate": "48000"},
        {"num_channels": 2},
        {"num_channels": True},
        {"sample_width": 1},
        {"encoding": "mp3"},
        {"container": "ogg"},
        {"unknown": "PRIVATE"},
    ],
)
def test_only_exact_documented_pcm_wav_configuration_is_accepted(changes):
    with pytest.raises(ValidationError) as error:
        GnaniTTSAudioConfig.model_validate(GnaniTTSAudioConfig().model_dump() | changes)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"speed": 0.5},
        {"speed": 2.0},
        {"speed": True},
        {"speed": "1.0"},
        {"api_key_id": "PRIVATE\r\nINJECTION"},
        {"api_key_id": ""},
        {"endpoint": "https://untrusted.invalid/"},
        {"documentation_reference": None},
        {"mappings": (mapping(), mapping())},
    ],
)
def test_unsafe_or_undocumented_backend_configuration_is_rejected(changes):
    with pytest.raises(ValidationError) as error:
        config(**changes)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("text", ["", " \n", "x" * 2001, "PRIVATE\x00", "\ud800", 123, b"PRIVATE"])
def test_invalid_or_oversized_text_cannot_reach_transport(text):
    adapter, request, calls = setup()
    with pytest.raises(GnaniTTSError) as error:
        adapter.synthesize(request=request.model_copy(update={"text": text}))
    safe(error.value)
    assert calls == []


@pytest.mark.parametrize("code", [400, 429, 500, 503, 401, 403, 301, 302, 307, 204])
def test_errors_and_redirects_are_sanitized_and_not_retried(code):
    adapter, request, calls = setup(
        httpx.Response(
            code, content=(KEY + TEXT).encode(), headers={"location": "https://untrusted.invalid/"}
        )
    )
    with pytest.raises(GnaniTTSError) as error:
        adapter.synthesize(request=request)
    safe(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectError,
        RuntimeError,
    ],
)
def test_transport_errors_and_timeouts_are_sanitized(failure):
    def failing(request):
        raise failure(KEY + TEXT)

    adapter, request, calls = setup(failing)
    with pytest.raises(GnaniTTSError) as error:
        adapter.synthesize(request=request)
    safe(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [b"", b"PRIVATE_NOT_WAV", b"RIFF" + b"\x00" * 40, wav()[:-2], b"x" * (MAX_AUDIO_BYTES + 1)],
)
def test_empty_invalid_truncated_or_oversized_output_is_rejected(body):
    adapter, request, _ = setup(
        httpx.Response(200, content=body, headers={"content-type": "audio/wav"})
    )
    with pytest.raises(GnaniTTSError) as error:
        adapter.synthesize(request=request)
    safe(error.value)


@pytest.mark.parametrize(
    "changes", [{"rate": 8000}, {"channels": 2}, {"width": 1}, {"seconds": 61}]
)
def test_actual_output_must_match_requested_audio_configuration(changes):
    adapter, request, _ = setup(
        httpx.Response(200, content=wav(**changes), headers={"content-type": "audio/wav"})
    )
    with pytest.raises(GnaniTTSError):
        adapter.synthesize(request=request)


@pytest.mark.parametrize("mime", [None, "application/octet-stream", "audio/wav", "audio/x-wav"])
def test_binary_audio_is_verified_even_without_specific_provider_mime(mime):
    headers = {} if mime is None else {"content-type": mime}
    adapter, request, _ = setup(httpx.Response(200, content=wav(), headers=headers))
    assert adapter.synthesize(request=request).audio == wav()


class Chunks(httpx.SyncByteStream):
    def __init__(self):
        self.closed = False
        self.reads = 0

    def __iter__(self):
        for _ in range(1000):
            self.reads += 1
            yield b"x" * 8192

    def close(self):
        self.closed = True


def test_streamed_output_limit_is_enforced_and_stream_closed():
    stream = Chunks()
    adapter, request, _ = setup(
        httpx.Response(200, stream=stream, headers={"content-type": "audio/wav"})
    )
    with pytest.raises(GnaniTTSError, match="response_too_large"):
        adapter.synthesize(request=request)
    assert stream.reads == MAX_AUDIO_BYTES // 8192 + 1 and stream.closed


@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": str(MAX_AUDIO_BYTES + 1)},
        {"content-length": "PRIVATE_INVALID"},
        {"content-type": "application/json"},
        {"content-encoding": "gzip"},
    ],
)
def test_invalid_response_headers_stop_before_body_read(headers):
    stream = Chunks()
    adapter, request, _ = setup(
        httpx.Response(200, stream=stream, headers={"content-type": "audio/wav"} | headers)
    )
    with pytest.raises(GnaniTTSError) as error:
        adapter.synthesize(request=request)
    safe(error.value)
    assert stream.reads == 0 and stream.closed


def test_content_length_disagreement_is_rejected():
    reply = httpx.Response(
        200, content=wav(), headers={"content-type": "audio/wav", "content-length": "1"}
    )
    adapter, request, _ = setup(reply)
    with pytest.raises(GnaniTTSError, match="invalid_response"):
        adapter.synthesize(request=request)


def test_duplicate_length_headers_are_rejected_without_read():
    stream = Chunks()
    adapter, request, _ = setup(
        httpx.Response(
            200,
            stream=stream,
            headers=[
                ("content-type", "audio/wav"),
                ("content-length", "1"),
                ("content-length", "2"),
            ],
        )
    )
    with pytest.raises(GnaniTTSError):
        adapter.synthesize(request=request)
    assert stream.closed and stream.reads == 0


def test_stream_failure_closes_audio_without_returning_partial_bytes():
    class Broken(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield wav()[:100]
            raise httpx.ReadTimeout(KEY + TEXT)

        def close(self):
            self.closed = True

    stream = Broken()
    adapter, request, calls = setup(
        httpx.Response(200, stream=stream, headers={"content-type": "audio/wav"})
    )
    with pytest.raises(GnaniTTSError, match="timeout") as error:
        adapter.synthesize(request=request)
    safe(error.value)
    assert stream.closed and len(calls) == 1


def test_wav_container_cannot_hide_extra_chunks():
    audio = bytearray(wav() + b"LIST\x04\x00\x00\x00NAME")
    struct.pack_into("<I", audio, 4, len(audio) - 8)
    adapter, request, _ = setup(
        httpx.Response(200, content=bytes(audio), headers={"content-type": "audio/wav"})
    )
    with pytest.raises(GnaniTTSError, match="invalid_response"):
        adapter.synthesize(request=request)


def test_configuration_revalidation_and_no_proxy_or_ambient_key(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://fictional-proxy.invalid:9999")
    monkeypatch.setenv("GNANI_API_KEY_ID", "FICTIONAL-IGNORED-AMBIENT-KEY")
    adapter, request, calls = setup()
    adapter.synthesize(request=request)
    assert calls[0].headers["X-API-Key-ID"] == KEY
    assert "Proxy-Authorization" not in calls[0].headers and "Authorization" not in calls[0].headers
    forged = config().model_copy(
        update={"audio_config": GnaniTTSAudioConfig().model_copy(update={"sample_rate": 8000})}
    )
    with pytest.raises(GnaniTTSError, match="configuration_unavailable"):
        GnaniTTSAdapter(forged)
    with pytest.raises(ValidationError):
        config().speed = 2.0
