"""Backend-only, disconnected REST STT/TTS adapters. No configuration or network on import.

Contract source: the documentation excerpt supplied for Phase 4 Part 3. Real
language codes must be independently confirmed and supplied via backend
configuration. The confirmed multipart selector defaults to format=verbatim.
No processing operation or credential is enabled by default. The reviewed registry
contains only the documented Hindi TTS profile as eligibility metadata.
Only canonical integer-PCM WAV is locally verifiable here; the other documented
containers remain rejected until reviewed parsers are available. TTS accepts only
the documented PCM WAV configuration and explicitly configured Hindi profile.
These separate structured boundaries leave the generic VoiceProvider Protocol
unchanged. No consent, nurse review or playback authorization is inferred.
"""

import json
import re
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StringConstraints,
    model_validator,
)

from app.integrations.voice_capabilities import VoiceCapabilityMapping, VoiceCapabilityRegistry
from app.schemas.patients import Language
from app.schemas.voice import (
    PlaybackMetadata,
    SpeechCapability,
    SpeechToTextRequest,
    TextToSpeechRequest,
    TranscriptDraft,
    VoiceCapabilities,
    validate_playback_metadata,
    validate_transcript_result,
)

ENDPOINT = "https://api.vachana.ai/stt/v3"
TTS_ENDPOINT = "https://api.vachana.ai/api/v1/tts/inference"
MAX_AUDIO_BYTES = 4 * 1024 * 1024  # Existing application ceiling, not a provider claim.
MAX_RESPONSE_BYTES = 65536
Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")]

# Eligibility from the documentation supplied in Phase 4 Part 4. No STT language
# identifiers have been confirmed. This registry has NO enablement flags.
GNANI_REVIEWED_CAPABILITIES = VoiceCapabilityRegistry(
    provider="gnani",
    mappings=(
        VoiceCapabilityMapping(
            operation="tts",
            language="hi",
            provider_language="hi-IN",
            voice="Nalini",
            model="timbre-v2.5",
            media_types=frozenset({"audio/wav"}),
            documentation_reference="User-supplied Gnani.ai REST TTS documentation, Phase 4 Part 4",
        ),
    ),
)


class GnaniLanguageMapping(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    language: Language
    language_code: Identifier


class GnaniSTTConfig(BaseModel):
    """Explicit backend injection only; never load administrative .env or frontend data.

    References identify reviewed documentation, not evidence verified by this model.
    No endpoint or multipart field-name override exists. The confirmed format
    selector defaults to verbatim; transcribe explicitly opts into provider ITN.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    api_key_id: SecretStr | None = Field(default=None, repr=False)
    enabled: StrictBool = False
    reviewed_capabilities: VoiceCapabilityRegistry = GNANI_REVIEWED_CAPABILITIES
    language_mappings: Annotated[tuple[GnaniLanguageMapping, ...], Field(max_length=8)] = ()
    language_documentation_reference: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)] | None
    ) = None
    output_format: Literal["verbatim", "transcribe"] = "verbatim"

    @model_validator(mode="after")
    def safe_configuration(self):
        if self.api_key_id is not None:
            key = self.api_key_id.get_secret_value()
            if not 1 <= len(key) <= 4096 or any(not 33 <= ord(char) <= 126 for char in key):
                raise ValueError("Invalid voice credential configuration")
        languages = [item.language for item in self.language_mappings]
        codes = [item.language_code for item in self.language_mappings]
        if len(set(languages)) != len(languages) or len(set(codes)) != len(codes):
            raise ValueError("Ambiguous language configuration")
        if self.reviewed_capabilities.provider != "gnani":
            raise ValueError("Unexpected speech capability registry")
        if self.enabled and not self.language_mappings:
            raise ValueError("Explicit STT mappings required")
        for item in self.language_mappings:
            eligible = self.reviewed_capabilities.eligible(
                operation="stt", language=item.language, provider_language=item.language_code
            )
            if "audio/wav" not in eligible.media_types:
                raise ValueError("Locally verifiable STT format is unavailable")
        reference = self.language_documentation_reference
        if self.language_mappings and (
            not reference or not reference.strip() or "\x00" in reference
        ):
            raise ValueError("Reviewed language documentation required")
        return self


class GnaniSTTError(Exception):
    """Never attach a provider response, HTTP request, URL or submitted data."""

    def __init__(self, code):
        allowed = {
            "configuration_unavailable",
            "capability_disabled",
            "unsupported_language",
            "invalid_audio",
            "audio_unverifiable",
            "invalid_request",
            "timeout",
            "provider_unavailable",
            "invalid_response",
            "response_too_large",
        }
        self.code = code if code in allowed else "provider_unavailable"
        super().__init__("Speech transcription unavailable (" + self.code + ")")


def _wav_duration(audio, filename, media_type):
    """Validate complete bounded PCM RIFF structure and derive duration from frames.

    Reject ancillary chunks (which may contain identifying metadata), compression,
    extensible WAV and unsupported containers instead of trusting MIME or suffix.
    These are local validation restrictions, not Gnani.ai format requirements.
    """
    if type(audio) is not bytes or not 44 <= len(audio) <= MAX_AUDIO_BYTES:
        raise GnaniSTTError("invalid_audio")
    if type(filename) is not str or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9]{1,5}", filename
    ):
        raise GnaniSTTError("invalid_audio")
    extension = filename.rsplit(".", 1)[1].lower()
    if extension not in {"wav", "mp3", "ogg", "flac", "aac", "m4a"}:
        raise GnaniSTTError("invalid_audio")
    if extension != "wav":
        raise GnaniSTTError("audio_unverifiable")
    if media_type != "audio/wav":
        raise GnaniSTTError("invalid_audio")
    if (
        audio[:4] != b"RIFF"
        or audio[8:16] != b"WAVEfmt "
        or struct.unpack_from("<I", audio, 4)[0] != len(audio) - 8
        or struct.unpack_from("<I", audio, 16)[0] != 16
        or audio[36:40] != b"data"
    ):
        raise GnaniSTTError("invalid_audio")
    encoding, channels, rate, byte_rate, alignment, bits = struct.unpack_from("<HHIIHH", audio, 20)
    size = struct.unpack_from("<I", audio, 40)[0]
    if (
        encoding != 1
        or channels not in {1, 2}
        or rate == 0
        or bits not in {8, 16, 24, 32}
        or alignment != channels * (bits // 8)
        or byte_rate != rate * alignment
        or size == 0
        or size % alignment
        or len(audio) != 44 + size + size % 2
        or size % 2
        and audio[-1:] != b"\x00"
    ):
        raise GnaniSTTError("invalid_audio")
    frames = size // alignment
    if frames > rate * 60:
        raise GnaniSTTError("invalid_audio")
    return (frames * 1000 + rate - 1) // rate


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _json_constant(value):
    raise ValueError


def _http_client(transport):
    return httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=5, read=10, write=10, pool=5),
    )


def _bounded_response(response, *, limit, error_type):
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise error_type("invalid_response")
    lengths = response.headers.get_list("content-length")
    if len(lengths) > 1 or lengths and not lengths[0].isascii():
        raise error_type("invalid_response")
    if lengths:
        if not lengths[0].isdigit() or len(lengths[0]) > 10:
            raise error_type("invalid_response")
        if int(lengths[0]) > limit:
            raise error_type("response_too_large")
    body = bytearray()
    for chunk in response.iter_bytes(chunk_size=8192):
        if len(body) + len(chunk) > limit:
            raise error_type("response_too_large")
        body.extend(chunk)
    if lengths and len(body) != int(lengths[0]):
        raise error_type("invalid_response")
    return bytes(body)


class GnaniSTTAdapter:
    """One bounded request, no retries, redirects, environment proxies or durable files.

    Callers must separately enforce verified nurse access and explicit approved
    voice-processing consent before invoking this low-level adapter. It is not
    registered with startup/routes. Transport injection is for offline testing.
    """

    def __init__(self, config: GnaniSTTConfig, *, transport: httpx.BaseTransport | None = None):
        try:
            if not isinstance(config, GnaniSTTConfig):
                raise ValueError
            self._config = GnaniSTTConfig.model_validate(config)
        except ValueError:
            raise GnaniSTTError("configuration_unavailable") from None
        self._transport = transport

    @property
    def capabilities(self):
        languages = (
            frozenset(mapping.language for mapping in self._config.language_mappings)
            if self._config.enabled
            else frozenset()
        )
        stt = (
            SpeechCapability(
                available=True, languages=languages, media_types=frozenset({"audio/wav"})
            )
            if languages
            else SpeechCapability()
        )
        return VoiceCapabilities(provider="gnani", stt=stt)

    def transcribe(
        self, *, audio: bytes, filename: str, request: SpeechToTextRequest
    ) -> TranscriptDraft:
        config = self._config
        if config.api_key_id is None:
            raise GnaniSTTError("configuration_unavailable")
        if not config.enabled:
            raise GnaniSTTError("capability_disabled")
        if not isinstance(request, SpeechToTextRequest):
            raise GnaniSTTError("invalid_request")
        mapping = next(
            (item for item in config.language_mappings if item.language == request.language), None
        )
        if mapping is None:
            raise GnaniSTTError("unsupported_language")
        try:
            request = SpeechToTextRequest.model_validate(
                request, context={"voice_capabilities": self.capabilities}
            )
        except ValueError:
            raise GnaniSTTError("invalid_request") from None
        try:
            config.reviewed_capabilities.require_enabled(
                enabled=config.enabled,
                operation="stt",
                language=request.language,
                provider_language=mapping.language_code,
                media_type=request.media_type,
            )
        except ValueError:
            raise GnaniSTTError("invalid_request") from None
        duration = _wav_duration(audio, filename, request.media_type)
        if request.byte_count != len(audio) or request.duration_ms != duration:
            raise GnaniSTTError("invalid_audio")
        try:
            with _http_client(self._transport) as client:
                with client.stream(
                    "POST",
                    ENDPOINT,
                    headers={
                        "X-API-Key-ID": config.api_key_id.get_secret_value(),
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    data={"language_code": mapping.language_code, "format": config.output_format},
                    files={"audio_file": ("audio.wav", audio, "audio/wav")},
                ) as response:
                    if not response.is_success:
                        raise GnaniSTTError("provider_unavailable")
                    if (
                        response.headers.get("content-type", "").split(";", 1)[0].lower()
                        != "application/json"
                        or response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        raise GnaniSTTError("invalid_response")
                    body = _bounded_response(
                        response, limit=MAX_RESPONSE_BYTES, error_type=GnaniSTTError
                    )
                    result = json.loads(
                        body.decode("utf-8"),
                        object_pairs_hook=_json_object,
                        parse_constant=_json_constant,
                    )
                    if not isinstance(result, dict) or result.get("success") is not True:
                        raise GnaniSTTError("invalid_response")
                    # Opaque provider correlation fields: bounded, discarded, never
                    # mistaken for the local request UUID or a trusted clinical clock.
                    for name in ("request_id", "timestamp"):
                        value = result.get(name)
                        if (
                            type(value) is not str
                            or not 1 <= len(value) <= 128
                            or not value.strip()
                            or "\x00" in value
                        ):
                            raise GnaniSTTError("invalid_response")
                        value.encode("utf-8")
                    draft = TranscriptDraft(
                        request_id=request.request_id,
                        patient_id=request.patient_id,
                        visit_id=request.visit_id,
                        language=request.language,
                        provider="gnani",
                        original_text=result.get("transcript"),
                        created_at=datetime.now(UTC),
                    )
                    return validate_transcript_result(
                        draft, request=request, capabilities=self.capabilities
                    )
        except GnaniSTTError:
            raise
        except httpx.TimeoutException:
            raise GnaniSTTError("timeout") from None
        except httpx.HTTPError:
            raise GnaniSTTError("provider_unavailable") from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise GnaniSTTError("invalid_response") from None
        except Exception:
            # Client initialization/stream/transport failures may contain request
            # details. Preserve no third-party exception text at this boundary.
            raise GnaniSTTError("provider_unavailable") from None


class GnaniTTSAudioConfig(BaseModel):
    """Only the supplied documented output tuple; no inferred alternative support."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    sample_rate: Annotated[int, Field(strict=True, ge=48000, le=48000)] = 48000
    num_channels: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    sample_width: Annotated[int, Field(strict=True, ge=2, le=2)] = 2
    encoding: Literal["linear_pcm"] = "linear_pcm"
    container: Literal["wav"] = "wav"


class GnaniTTSMapping(BaseModel):
    """An explicit documented tuple, not a production-enabled capability."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    language: Language
    provider_language: Identifier
    voice: Identifier
    model: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_.-]{1,64}$")]

    @model_validator(mode="after")
    def documented_tuple_only(self):
        if (self.language, self.provider_language, self.voice, self.model) != (
            "hi",
            "hi-IN",
            "Nalini",
            "timbre-v2.5",
        ):
            raise ValueError("TTS profile is not documented for this adapter")
        return self


class GnaniTTSConfig(BaseModel):
    """Backend-only injection; no .env loading and no profile enabled by default.

    Documentation references must be verified by the operator outside this model.
    Only speed 1.0 was supplied as documented, so other speeds remain unavailable.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    api_key_id: SecretStr | None = Field(default=None, repr=False)
    enabled: StrictBool = False
    reviewed_capabilities: VoiceCapabilityRegistry = GNANI_REVIEWED_CAPABILITIES
    mappings: Annotated[tuple[GnaniTTSMapping, ...], Field(max_length=8)] = ()
    documentation_reference: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)] | None
    ) = None
    speed: Annotated[float, Field(strict=True, ge=1.0, le=1.0)] = 1.0
    audio_config: GnaniTTSAudioConfig = GnaniTTSAudioConfig()

    @model_validator(mode="after")
    def explicit_profile(self):
        # Reuse backend credential validation; never derive STT language support.
        GnaniSTTConfig(api_key_id=self.api_key_id)
        if len({mapping.language for mapping in self.mappings}) != len(self.mappings):
            raise ValueError("Ambiguous TTS language configuration")
        if self.reviewed_capabilities.provider != "gnani":
            raise ValueError("Unexpected speech capability registry")
        if self.enabled and not self.mappings:
            raise ValueError("Explicit TTS mappings required")
        for item in self.mappings:
            eligible = self.reviewed_capabilities.eligible(
                operation="tts",
                language=item.language,
                provider_language=item.provider_language,
                voice=item.voice,
                model=item.model,
            )
            if "audio/wav" not in eligible.media_types:
                raise ValueError("Locally verifiable TTS format is unavailable")
        reference = self.documentation_reference
        if self.mappings and (not reference or not reference.strip() or "\x00" in reference):
            raise ValueError("Reviewed TTS documentation required")
        return self


class GnaniTTSError(Exception):
    def __init__(self, code):
        allowed = {
            "configuration_unavailable",
            "capability_disabled",
            "unsupported_language",
            "invalid_request",
            "timeout",
            "provider_unavailable",
            "invalid_response",
            "response_too_large",
        }
        self.code = code if code in allowed else "provider_unavailable"
        super().__init__("Speech synthesis unavailable (" + self.code + ")")


@dataclass(frozen=True, slots=True)
class GnaniTTSResult:
    """Ephemeral validated bytes only, not authorization to play clinical content."""

    audio: bytes = field(repr=False)
    metadata: PlaybackMetadata

    @property
    def playback_authorized(self):
        return False


class GnaniTTSAdapter:
    """Disconnected TTS boundary. No auto-play, storage, consent or nurse-review claim.

    Authenticated patient access, explicit voice-processing consent and nurse
    approval of synthesis text must be enforced by a future service before calling.
    The application 4 MiB/60-second audio ceilings are NOT provider TTS limits.
    """

    def __init__(self, config: GnaniTTSConfig, *, transport: httpx.BaseTransport | None = None):
        try:
            if not isinstance(config, GnaniTTSConfig):
                raise ValueError
            self._config = GnaniTTSConfig.model_validate(config)
        except ValueError:
            raise GnaniTTSError("configuration_unavailable") from None
        self._transport = transport

    @property
    def capabilities(self):
        languages = (
            frozenset(item.language for item in self._config.mappings)
            if self._config.enabled
            else frozenset()
        )
        tts = (
            SpeechCapability(
                available=True, languages=languages, media_types=frozenset({"audio/wav"})
            )
            if languages
            else SpeechCapability()
        )
        return VoiceCapabilities(provider="gnani", tts=tts)

    def synthesize(self, *, request: TextToSpeechRequest) -> GnaniTTSResult:
        config = self._config
        if config.api_key_id is None:
            raise GnaniTTSError("configuration_unavailable")
        if not config.enabled:
            raise GnaniTTSError("capability_disabled")
        if not isinstance(request, TextToSpeechRequest):
            raise GnaniTTSError("invalid_request")
        mapping = next(
            (item for item in config.mappings if item.language == request.language), None
        )
        if mapping is None:
            raise GnaniTTSError("unsupported_language")
        try:
            request = TextToSpeechRequest.model_validate(
                request, context={"voice_capabilities": self.capabilities}
            )
            config.reviewed_capabilities.require_enabled(
                enabled=config.enabled,
                operation="tts",
                language=request.language,
                provider_language=mapping.provider_language,
                voice=mapping.voice,
                model=mapping.model,
                media_type=request.media_type,
            )
        except ValueError:
            raise GnaniTTSError("invalid_request") from None
        try:
            with _http_client(self._transport) as client:
                with client.stream(
                    "POST",
                    TTS_ENDPOINT,
                    headers={
                        "X-API-Key-ID": config.api_key_id.get_secret_value(),
                        "Accept": "audio/wav",
                        "Accept-Encoding": "identity",
                    },
                    json={
                        "text": request.text,
                        "voice": mapping.voice,
                        "model": mapping.model,
                        "language": mapping.provider_language,
                        "speed": config.speed,
                        "audio_config": config.audio_config.model_dump(mode="json"),
                    },
                ) as response:
                    if response.status_code != 200:
                        raise GnaniTTSError("provider_unavailable")
                    # The supplied contract specifies binary audio, not an exact
                    # Content-Type. MIME is advisory; a full PCM WAV check is mandatory.
                    media_type = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    if media_type and media_type not in {
                        "audio/wav",
                        "audio/x-wav",
                        "audio/vnd.wave",
                        "application/octet-stream",
                    }:
                        raise GnaniTTSError("invalid_response")
                    audio = _bounded_response(
                        response, limit=MAX_AUDIO_BYTES, error_type=GnaniTTSError
                    )
                    try:
                        duration = _wav_duration(audio, "audio.wav", "audio/wav")
                    except GnaniSTTError:
                        raise GnaniTTSError("invalid_response") from None
                    _, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", audio, 20)
                    if (
                        channels != config.audio_config.num_channels
                        or rate != config.audio_config.sample_rate
                        or bits != config.audio_config.sample_width * 8
                    ):
                        raise GnaniTTSError("invalid_response")
                    metadata = PlaybackMetadata(
                        request_id=request.request_id,
                        patient_id=request.patient_id,
                        visit_id=request.visit_id,
                        language=request.language,
                        provider="gnani",
                        media_type="audio/wav",
                        byte_count=len(audio),
                        duration_ms=duration,
                    )
                    metadata = validate_playback_metadata(
                        metadata, request=request, capabilities=self.capabilities
                    )
                    return GnaniTTSResult(audio=audio, metadata=metadata)
        except GnaniTTSError:
            raise
        except httpx.TimeoutException:
            raise GnaniTTSError("timeout") from None
        except httpx.HTTPError:
            raise GnaniTTSError("provider_unavailable") from None
        except (ValueError, TypeError, UnicodeError, RecursionError, struct.error):
            raise GnaniTTSError("invalid_response") from None
        except Exception:
            raise GnaniTTSError("provider_unavailable") from None
