"""Offline provider-neutral voice contracts; no recording, I/O or clinical authority.

Capabilities must come from trusted server context, not request JSON. Declarations
are not verified vendor documentation, recording consent, translation support or
permission to send data. The limits below are defensive application ceilings, NOT
Gnani.ai limits. Future integration must enforce actual bytes/duration and stricter
provider limits, authenticated patient/visit access, consent and audited persistence.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.core.security import Principal
from app.schemas.patients import Language

Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")]
AudioMediaType = Annotated[
    str, StringConstraints(strict=True, pattern=r"^audio/[a-z0-9.+-]{1,50}$")
]
AudioBytes = Annotated[int, Field(strict=True, ge=1, le=4 * 1024 * 1024)]
DurationMs = Annotated[int, Field(strict=True, ge=1, le=60000)]
TranscriptText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000)]
SpeechText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
ReviewState = Literal["unverified", "nurse_corrected", "nurse_confirmed"]


class VoiceContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )

    @field_validator("*", mode="before")
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str):
            if not value.strip() or "\x00" in value:
                raise ValueError("Invalid voice text field")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("Invalid voice text encoding") from None
        return value

    @field_validator("created_at", "reviewed_at", mode="before", check_fields=False)
    @classmethod
    def explicit_timestamp(cls, value):
        if type(value) is str:
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("Aware ISO timestamp required") from None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Aware ISO timestamp required")
        return value


class SpeechCapability(VoiceContract):
    available: StrictBool = False
    languages: Annotated[frozenset[Language], Field(max_length=8)] = frozenset()
    media_types: Annotated[frozenset[AudioMediaType], Field(max_length=32)] = frozenset()

    @model_validator(mode="after")
    def explicit_support(self):
        if self.available != bool(self.languages and self.media_types):
            raise ValueError("Explicit speech capability metadata required")
        if not self.available and (self.languages or self.media_types):
            raise ValueError("Unavailable speech capability must be empty")
        return self


class VoiceCapabilities(VoiceContract):
    provider: Identifier
    stt: SpeechCapability = SpeechCapability()
    tts: SpeechCapability = SpeechCapability()


GNANI_CAPABILITIES = VoiceCapabilities(provider="gnani")


def _supported(capabilities, operation, language, media_type):
    if not isinstance(capabilities, VoiceCapabilities):
        raise ValueError("Trusted voice capabilities required")
    try:
        capabilities = VoiceCapabilities.model_validate(capabilities)
    except ValueError:
        raise ValueError("Invalid trusted voice capabilities") from None
    selected = getattr(capabilities, operation)
    if (
        not selected.available
        or language not in selected.languages
        or media_type not in selected.media_types
    ):
        raise ValueError("Speech capability unavailable")
    return capabilities


class VoiceIdentity(VoiceContract):
    # Server-bound correlation/path metadata, not proof of access or consent.
    request_id: UUID
    patient_id: UUID = Field(repr=False)
    visit_id: UUID = Field(repr=False)
    language: Language

    @field_validator("request_id", "patient_id", "visit_id", mode="before")
    @classmethod
    def canonical_uuid(cls, value):
        if isinstance(value, UUID):
            return value
        if type(value) is not str:
            raise ValueError("UUID required")
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError("UUID required") from None
        if str(parsed) != value.lower():
            raise ValueError("Canonical UUID required")
        return parsed


class SpeechToTextRequest(VoiceIdentity):
    media_type: AudioMediaType
    byte_count: AudioBytes
    duration_ms: DurationMs

    @model_validator(mode="after")
    def available_language(self, info: ValidationInfo):
        _supported(
            (info.context or {}).get("voice_capabilities"), "stt", self.language, self.media_type
        )
        return self


class TranscriptDraft(VoiceIdentity):
    """Internal normalized STT result, not a vendor response schema or translation."""

    provider: Identifier
    original_text: TranscriptText = Field(repr=False)
    created_at: AwareDatetime
    state: Literal["unverified"] = "unverified"


class TranscriptReviewAction(VoiceContract):
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=10)]
    action: Literal["correct", "confirm"]
    corrected_text: TranscriptText | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def correction_is_explicit(self):
        if (self.action == "correct") != (self.corrected_text is not None):
            raise ValueError("Separate correction and confirmation required")
        return self


class TranscriptReviewEvent(VoiceContract):
    """Server-created historical metadata; never accept reviewer identity from HTTP input."""

    revision: Annotated[int, Field(strict=True, ge=1, le=10)]
    nurse_id: UUID = Field(repr=False)
    reviewed_at: AwareDatetime
    action: TranscriptReviewAction


class ReviewedTranscript(VoiceContract):
    """Immutable review history. Nurse confirmation is not clinician approval."""

    draft: TranscriptDraft = Field(repr=False)
    reviews: Annotated[tuple[TranscriptReviewEvent, ...], Field(max_length=10)] = Field(
        default=(), repr=False
    )
    state: ReviewState = "unverified"

    @model_validator(mode="after")
    def chronological_history(self):
        previous = self.draft.created_at
        confirmed = False
        for expected, event in enumerate(self.reviews, start=1):
            if (
                confirmed
                or event.revision != expected
                or event.action.expected_revision != expected - 1
                or event.reviewed_at < previous
            ):
                raise ValueError("Invalid transcript review history")
            previous = event.reviewed_at
            confirmed = event.action.action == "confirm"
        expected_state = (
            "unverified"
            if not self.reviews
            else "nurse_confirmed"
            if confirmed
            else "nurse_corrected"
        )
        if self.state != expected_state:
            raise ValueError("Inconsistent transcript review state")
        return self

    @property
    def current_text(self) -> str:
        for event in reversed(self.reviews):
            if event.action.action == "correct":
                return event.action.corrected_text
        return self.draft.original_text


def review_transcript(current, action, *, principal: Principal, at: datetime) -> ReviewedTranscript:
    """Pure transition using an ALREADY VERIFIED nurse Principal supplied by server code.

    This does not verify JWTs/active assignments, grant consent, persist symptoms or
    authorize clinical use. Future callers must use existing authentication and DB
    checks. Confirmed snapshots are terminal; future amendments need a new workflow.
    """
    if (
        not isinstance(principal, Principal)
        or principal.role != "nurse"
        or not isinstance(principal.id, UUID)
    ):
        raise ValueError("Verified nurse principal required")
    current = ReviewedTranscript.model_validate(current)
    action = TranscriptReviewAction.model_validate(action)
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Aware review timestamp required")
    if current.state == "nurse_confirmed" or action.expected_revision != len(current.reviews):
        raise ValueError("Transcript review conflict")
    event = TranscriptReviewEvent(
        revision=len(current.reviews) + 1, nurse_id=principal.id, reviewed_at=at, action=action
    )
    return ReviewedTranscript(
        draft=current.draft,
        reviews=(*current.reviews, event),
        state="nurse_corrected" if action.action == "correct" else "nurse_confirmed",
    )


class TextToSpeechRequest(VoiceIdentity):
    # Does not attest nurse approval of the text, voice consent or translation.
    text: SpeechText = Field(repr=False)
    media_type: AudioMediaType

    @model_validator(mode="after")
    def available_language(self, info: ValidationInfo):
        _supported(
            (info.context or {}).get("voice_capabilities"), "tts", self.language, self.media_type
        )
        return self


class PlaybackMetadata(VoiceIdentity):
    provider: Identifier
    media_type: AudioMediaType
    byte_count: AudioBytes
    duration_ms: DurationMs


def validate_transcript_result(value, *, request, capabilities) -> TranscriptDraft:
    """Bind internal normalized result to the exact STT request; no language fallback."""
    if not isinstance(request, SpeechToTextRequest):
        raise ValueError("Validated speech request required")
    request = SpeechToTextRequest.model_validate(
        request, context={"voice_capabilities": capabilities}
    )
    result = TranscriptDraft.model_validate(value)
    trusted = _supported(capabilities, "stt", request.language, request.media_type)
    if result.provider != trusted.provider or any(
        getattr(result, key) != getattr(request, key)
        for key in ("request_id", "patient_id", "visit_id", "language")
    ):
        raise ValueError("Transcript request mismatch")
    return result


def validate_playback_metadata(value, *, request, capabilities) -> PlaybackMetadata:
    """No audio URL, raw bytes, storage identifier or autoplay instruction is accepted."""
    if not isinstance(request, TextToSpeechRequest):
        raise ValueError("Validated speech request required")
    request = TextToSpeechRequest.model_validate(
        request, context={"voice_capabilities": capabilities}
    )
    result = PlaybackMetadata.model_validate(value)
    trusted = _supported(capabilities, "tts", request.language, request.media_type)
    if result.provider != trusted.provider or any(
        getattr(result, key) != getattr(request, key)
        for key in ("request_id", "patient_id", "visit_id", "language", "media_type")
    ):
        raise ValueError("Playback request mismatch")
    return result
