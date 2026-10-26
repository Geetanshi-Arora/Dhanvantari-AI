"""Offline MockTransport only. Synthetic silence and fictional metadata/credentials.

The fictional language code below does NOT claim Gnani.ai language support.
No actual audio recording, real key, endpoint call, clinical consent or approval.
"""

import io
import json
import logging
import socket
import struct
import wave
from uuid import UUID

import httpx
import pytest
from app.integrations.gnani import (
    ENDPOINT,
    MAX_AUDIO_BYTES,
    MAX_RESPONSE_BYTES,
    GnaniLanguageMapping,
    GnaniSTTAdapter,
    GnaniSTTConfig,
    GnaniSTTError,
)
from app.integrations.voice_capabilities import VoiceCapabilityMapping, VoiceCapabilityRegistry
from app.schemas.voice import GNANI_CAPABILITIES, SpeechToTextRequest
from pydantic import SecretStr, ValidationError

UID = UUID("10000000-0000-4000-8000-000000000001")
KEY = "FICTIONAL-MOCK-KEY-NOT-A-CREDENTIAL"
PRIVATE = "FICTIONAL-PRIVATE-TRANSCRIPT-NOT-PATIENT-DATA"
TEXT = PRIVATE + "\nExact fixture wording.\n"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Real network operations forbidden")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def wav(seconds=1, rate=8000):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * (rate * seconds))
    return output.getvalue()


def config(**changes):
    # Explicit test-only enablement and registry; these codes are NOT vendor claims.
    reviewed = VoiceCapabilityRegistry(
        provider="gnani",
        mappings=(
            VoiceCapabilityMapping(
                operation="stt",
                language="hi",
                provider_language="fictional-hi",
                media_types=frozenset({"audio/wav"}),
                documentation_reference="FICTIONAL-MOCK-DOCUMENTATION-ONLY",
            ),
        ),
    )
    return GnaniSTTConfig.model_validate(
        dict(
            api_key_id=SecretStr(KEY),
            enabled=True,
            reviewed_capabilities=reviewed,
            language_mappings=(GnaniLanguageMapping(language="hi", language_code="fictional-hi"),),
            language_documentation_reference="FICTIONAL-MOCK-DOCUMENTATION-ONLY",
        )
        | changes
    )


def metadata(adapter, audio, **changes):
    return SpeechToTextRequest.model_validate(
        dict(
            request_id=UID,
            patient_id=UID,
            visit_id=UID,
            language="hi",
            media_type="audio/wav",
            byte_count=len(audio),
            duration_ms=1000,
        )
        | changes,
        context={"voice_capabilities": adapter.capabilities},
    )


def response(**changes):
    return httpx.Response(
        200,
        json=dict(
            success=True,
            request_id="fictional-provider-id",
            timestamp="fixture-provider-timestamp",
            transcript=TEXT,
        )
        | changes,
    )


def run_response(reply, **config_changes):
    requests = []

    def handler(request):
        requests.append(request)
        return reply(request) if callable(reply) else reply

    adapter = GnaniSTTAdapter(config(**config_changes), transport=httpx.MockTransport(handler))
    audio = wav()
    return adapter, audio, metadata(adapter, audio), requests


def invoke(adapter, audio, request, filename="fixture.wav"):
    return adapter.transcribe(audio=audio, filename=filename, request=request)


def assert_safe(error):
    assert isinstance(error, GnaniSTTError)
    assert KEY not in str(error) and PRIVATE not in str(error) and "PRIVATE_AUDIO" not in str(error)
    assert "https://" not in str(error)


def test_fixed_endpoint_authentication_multipart_verbatim_and_unverified_output(caplog):
    caplog.set_level(logging.DEBUG)
    adapter, audio, request, requests = run_response(
        response(state="nurse_confirmed", approved=True)
    )
    draft = invoke(adapter, audio, request, filename="FictionalPatientName.wav")
    assert len(requests) == 1
    outgoing = requests[0]
    assert str(outgoing.url) == ENDPOINT and outgoing.method == "POST"
    assert outgoing.headers["X-API-Key-ID"] == KEY
    assert outgoing.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = outgoing.read()
    assert b'name="audio_file"; filename="audio.wav"' in body
    assert b"FictionalPatientName" not in body
    assert b'name="language_code"\r\n\r\nfictional-hi' in body
    assert b'name="format"\r\n\r\nverbatim' in body
    assert b"Content-Type: audio/wav" in body
    assert audio in body
    assert str(UID).encode() not in body  # No patient/visit/request identifiers sent.
    assert outgoing.extensions["timeout"] == {"connect": 5, "read": 10, "write": 10, "pool": 5}
    assert draft.state == "unverified" and draft.original_text == TEXT and draft.language == "hi"
    assert draft.request_id == UID and draft.provider == "gnani"
    assert KEY not in caplog.text and PRIVATE not in caplog.text
    assert KEY not in repr(adapter._config)
    assert not adapter.capabilities.tts.available


def test_transcribe_itn_mode_requires_explicit_backend_configuration():
    adapter, audio, request, requests = run_response(response(), output_format="transcribe")
    invoke(adapter, audio, request)
    assert b'name="format"\r\n\r\ntranscribe' in requests[0].read()


def test_missing_credentials_and_empty_default_languages_cannot_send():
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or response())
    empty = GnaniSTTAdapter(GnaniSTTConfig(), transport=transport)
    assert not empty.capabilities.stt.available and not empty.capabilities.tts.available
    assert not GNANI_CAPABILITIES.stt.available
    with pytest.raises(GnaniSTTError, match="configuration_unavailable") as error:
        empty.transcribe(audio=b"PRIVATE_AUDIO", filename="fixture.wav", request=None)
    assert_safe(error.value)
    enabled = GnaniSTTAdapter(config(), transport=transport)
    request = metadata(enabled, wav())
    disabled = GnaniSTTAdapter(GnaniSTTConfig(api_key_id=SecretStr(KEY)), transport=transport)
    with pytest.raises(GnaniSTTError, match="capability_disabled"):
        invoke(disabled, wav(), request)
    assert calls == []


@pytest.mark.parametrize(
    "language", ["en", "bn", "ta", "te", "mr", "gu", "pa", "fr", "auto", "hi-IN"]
)
def test_no_silent_language_fallback(language):
    adapter, audio, request, requests = run_response(response())
    forged = request.model_copy(update={"language": language})
    with pytest.raises(GnaniSTTError):
        invoke(adapter, audio, forged)
    assert not requests


@pytest.mark.parametrize(
    "filename",
    [
        "fixture.mp3",
        "fixture.ogg",
        "fixture.flac",
        "fixture.aac",
        "fixture.m4a",
        "fixture.exe",
        "../fixture.wav",
        "fixture.wav.exe",
        "private name.wav",
        "x" * 65 + ".wav",
    ],
)
def test_unsupported_or_unverifiable_container_and_unsafe_extension_never_send(filename):
    adapter, audio, request, requests = run_response(response())
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request, filename)
    assert_safe(error.value)
    assert not requests


@pytest.mark.parametrize(
    "mutation",
    [
        "not_audio",
        "truncated",
        "riff_size",
        "format",
        "compressed",
        "channels",
        "rate",
        "byte_rate",
        "alignment",
        "bits",
        "data_size",
        "extra_chunk",
    ],
)
def test_wav_contents_are_inspected_not_just_extension(mutation):
    adapter, original, request, requests = run_response(response())
    audio = bytearray(original)
    if mutation == "not_audio":
        audio[:12] = b"PRIVATE_AUDIO"
    elif mutation == "truncated":
        audio = audio[:-2]
    elif mutation == "riff_size":
        struct.pack_into("<I", audio, 4, 0)
    elif mutation == "format":
        audio[12:16] = b"JUNK"
    elif mutation == "compressed":
        struct.pack_into("<H", audio, 20, 3)
    elif mutation == "channels":
        struct.pack_into("<H", audio, 22, 0)
    elif mutation == "rate":
        struct.pack_into("<I", audio, 24, 0)
    elif mutation == "byte_rate":
        struct.pack_into("<I", audio, 28, 0)
    elif mutation == "alignment":
        struct.pack_into("<H", audio, 32, 3)
    elif mutation == "bits":
        struct.pack_into("<H", audio, 34, 7)
    elif mutation == "data_size":
        struct.pack_into("<I", audio, 40, len(audio))
    elif mutation == "extra_chunk":
        audio.extend(b"LIST\x00\x00\x00\x00")
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, bytes(audio), request)
    assert_safe(error.value)
    assert not requests


@pytest.mark.parametrize(
    "changes", [{"duration_ms": 999}, {"byte_count": 1}, {"media_type": "audio/mpeg"}]
)
def test_claimed_metadata_cannot_override_measured_audio(changes):
    adapter, audio, request, requests = run_response(response())
    with pytest.raises(GnaniSTTError):
        invoke(adapter, audio, request.model_copy(update=changes))
    assert not requests


def test_audio_size_and_measured_duration_limits():
    adapter, audio, request, requests = run_response(response())
    for invalid in (b"x" * (MAX_AUDIO_BYTES + 1), wav(seconds=61), b"", bytearray(audio)):
        with pytest.raises(GnaniSTTError):
            invoke(adapter, invalid, request)
    assert not requests
    audio = wav(seconds=60)
    valid = metadata(adapter, audio, duration_ms=60000)
    assert invoke(adapter, audio, valid).state == "unverified"


@pytest.mark.parametrize(
    "error_class",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectError,
        httpx.ReadError,
    ],
)
def test_timeouts_and_network_failures_are_sanitized_without_retry(error_class):
    def failing(request):
        raise error_class(KEY + PRIVATE, request=request)

    adapter, audio, request, requests = run_response(failing)
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403, 413, 429, 500, 503])
def test_provider_errors_and_redirects_never_leak_or_retry(status):
    adapter, audio, request, requests = run_response(
        httpx.Response(
            status, text=PRIVATE + KEY, headers={"Location": "https://untrusted.invalid/"}
        )
    )
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"success": False},
        {"success": "true"},
        {"request_id": None},
        {"request_id": "x" * 129},
        {"timestamp": None},
        {"transcript": None},
        {"transcript": {}},
        {"transcript": ""},
        {"transcript": " "},
        {"transcript": PRIVATE + "\x00"},
        {"transcript": "x" * 8001},
    ],
)
def test_malformed_provider_response_is_sanitized(changes):
    adapter, audio, request, _ = run_response(response(**changes))
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)


@pytest.mark.parametrize("missing", ["success", "request_id", "timestamp", "transcript"])
def test_missing_documented_response_field_is_rejected(missing):
    data = dict(success=True, request_id="fictional", timestamp="opaque", transcript=TEXT)
    del data[missing]
    adapter, audio, request, _ = run_response(httpx.Response(200, json=data))
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"success":true,"success":false}',
        b'{"transcript":NaN}',
        b"[]",
        b"\xff",
        b"PRIVATE_NOT_JSON",
    ],
)
def test_ambiguous_or_invalid_json_is_rejected(body):
    adapter, audio, request, _ = run_response(
        httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)


class Chunks(httpx.SyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for _ in range(100):
            self.reads += 1
            yield b"x" * 8192

    def close(self):
        self.closed = True


def test_streamed_response_is_bounded_and_closed():
    stream = Chunks()
    adapter, audio, request, _ = run_response(
        httpx.Response(200, stream=stream, headers={"content-type": "application/json"})
    )
    with pytest.raises(GnaniSTTError, match="response_too_large"):
        invoke(adapter, audio, request)
    assert stream.reads == MAX_RESPONSE_BYTES // 8192 + 1
    assert stream.closed


@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": str(MAX_RESPONSE_BYTES + 1)},
        {"content-length": "invalid"},
        {"content-type": "text/html"},
        {"content-encoding": "gzip"},
    ],
)
def test_response_headers_are_validated_before_read(headers):
    stream = Chunks()
    adapter, audio, request, _ = run_response(
        httpx.Response(200, stream=stream, headers={"content-type": "application/json"} | headers)
    )
    with pytest.raises(GnaniSTTError):
        invoke(adapter, audio, request)
    assert stream.reads == 0 and stream.closed


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "https://untrusted.invalid/"},
        {"output_format": "unknown"},
        {"api_key_id": "PRIVATE\r\nINJECTION"},
        {"api_key_id": ""},
    ],
)
def test_unsafe_backend_configuration_has_sanitized_validation(changes):
    data = config().model_dump() | changes
    with pytest.raises(ValidationError) as error:
        GnaniSTTConfig.model_validate(data)
    assert "PRIVATE" not in str(error.value)


def test_documentation_and_unique_mappings_required():
    mapping = GnaniLanguageMapping(language="hi", language_code="fictional-hi")
    with pytest.raises(ValidationError):
        GnaniSTTConfig(language_mappings=(mapping,))
    with pytest.raises(ValidationError):
        GnaniSTTConfig(
            language_mappings=(mapping, mapping), language_documentation_reference="FICTIONAL"
        )
    with pytest.raises(ValidationError):
        mapping.language_code = "other"
    with pytest.raises(ValidationError):
        config().output_format = "transcribe"


def test_unexpected_transport_error_is_sanitized():
    def failing(request):
        raise RuntimeError(KEY + PRIVATE)

    adapter, audio, request, requests = run_response(failing)
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)
    assert len(requests) == 1


def test_environment_credentials_and_proxies_are_not_forwarded(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://fictional-proxy.invalid:9999")
    monkeypatch.setenv("GNANI_API_KEY_ID", "FICTIONAL-IGNORED-AMBIENT-KEY")
    adapter, audio, request, requests = run_response(response())
    invoke(adapter, audio, request)
    assert requests[0].headers["X-API-Key-ID"] == KEY
    assert "Proxy-Authorization" not in requests[0].headers
    assert "Authorization" not in requests[0].headers


@pytest.mark.parametrize("field", ["request_id", "timestamp"])
def test_malformed_provider_metadata_encoding_is_rejected(field):
    adapter, audio, request, _ = run_response(
        httpx.Response(
            200,
            content=json.dumps(
                dict(success=True, request_id="fixture", timestamp="fixture", transcript=TEXT)
                | {field: "\ud800"}
            ).encode(),
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(GnaniSTTError) as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)


def test_content_length_disagreement_is_rejected():
    reply = response()
    reply.headers["content-length"] = "1"
    adapter, audio, request, _ = run_response(reply)
    with pytest.raises(GnaniSTTError, match="invalid_response"):
        invoke(adapter, audio, request)


def test_duplicate_content_length_is_rejected_without_body_read():
    stream = Chunks()
    adapter, audio, request, _ = run_response(
        httpx.Response(
            200,
            stream=stream,
            headers=[
                ("content-type", "application/json"),
                ("content-length", "1"),
                ("content-length", "2"),
            ],
        )
    )
    with pytest.raises(GnaniSTTError, match="invalid_response"):
        invoke(adapter, audio, request)
    assert stream.closed and stream.reads == 0


def test_stream_timeout_closes_response_and_preserves_privacy():
    class Broken(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'{"fixture":'
            raise httpx.ReadTimeout(KEY + PRIVATE)

        def close(self):
            self.closed = True

    stream = Broken()
    adapter, audio, request, requests = run_response(
        httpx.Response(200, stream=stream, headers={"content-type": "application/json"})
    )
    with pytest.raises(GnaniSTTError, match="timeout") as error:
        invoke(adapter, audio, request)
    assert_safe(error.value)
    assert stream.closed and len(requests) == 1


def test_extra_wav_metadata_is_rejected_even_with_correct_container_length():
    adapter, audio, request, requests = run_response(response())
    changed = bytearray(audio + b"LIST\x04\x00\x00\x00NAME")
    struct.pack_into("<I", changed, 4, len(changed) - 8)
    forged = request.model_copy(update={"byte_count": len(changed)})
    with pytest.raises(GnaniSTTError, match="invalid_audio"):
        invoke(adapter, bytes(changed), forged)
    assert not requests
