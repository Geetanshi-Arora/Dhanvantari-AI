"""Disabled, offline-only voice HTTP scaffolding.

No provider, consent policy or workflow store is constructed here. Enabling the
backend gate is insufficient without a reviewed single-process state binding.
Callbacks may retain bounded, short-lived server handles in memory only; they must
not persist speech, audio or transcripts. No cross-worker/resume guarantee exists.
The current application supplies NONE of these bindings. Browser playback and
clinical use are never authorized by these routes.
"""

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field, StringConstraints

from app.core.security import Principal, require_role
from app.schemas.patients import Language
from app.schemas.voice import (
    GNANI_CAPABILITIES,
    AudioBytes,
    AudioMediaType,
    DurationMs,
    ReviewedTranscript,
    SpeechText,
    SpeechToTextRequest,
    TextToSpeechRequest,
    TranscriptDraft,
    TranscriptReviewAction,
    VoiceCapabilities,
    VoiceContract,
)
from app.services.voice import VoiceService, VoiceServiceError, VoiceTranscriptHandle

router = APIRouter(prefix="/api/v1/voice", tags=["voice (disabled)"])
nurse = require_role("nurse")
ROOT = "/patients/{patient_id}/visits/{visit_id}"
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_BASE64 = 4 * ((MAX_AUDIO_BYTES + 2) // 3)


@dataclass(frozen=True)
class VoiceWorkflows:
    """Trusted ephemeral lifecycle callbacks; never client-supplied handles.

    remember must reject capacity/expiry conflicts; replace must compare-and-swap
    the exact old object and reject stale revisions. resolve must expire handles.
    Binding implementation, bounded lifetime and single-process deployment require
    review before activation. Offline tests supply an in-memory fake only.
    """

    remember: Callable[[VoiceTranscriptHandle], None]
    resolve: Callable[[UUID], VoiceTranscriptHandle | None]
    replace: Callable[[VoiceTranscriptHandle, VoiceTranscriptHandle], None]


@dataclass(frozen=True)
class VoiceRouteBindings:
    enabled: bool = False
    single_process: bool = False
    service: VoiceService | None = None
    capabilities: VoiceCapabilities = GNANI_CAPABILITIES
    workflows: VoiceWorkflows | None = None


class TranscriptionBody(VoiceContract):
    language: Language
    media_type: AudioMediaType
    byte_count: AudioBytes
    duration_ms: DurationMs
    filename: Annotated[
        str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9]{1,5}$")
    ]
    audio_base64: Annotated[
        str, StringConstraints(strict=True, min_length=4, max_length=MAX_BASE64)
    ] = Field(repr=False)


class SynthesisBody(VoiceContract):
    language: Language
    media_type: AudioMediaType
    text: SpeechText = Field(repr=False)


class ReviewBody(TranscriptReviewAction):
    language: Language


def unavailable():
    return HTTPException(503, "Voice workflow unavailable")


def bindings(request: Request, principal: Principal = Depends(nurse)) -> VoiceRouteBindings:
    value = getattr(request.app.state, "voice_bindings", None)
    if (
        type(value) is not VoiceRouteBindings
        or value.enabled is not True
        or value.single_process is not True
        or not isinstance(value.service, VoiceService)
        or not isinstance(value.capabilities, VoiceCapabilities)
        or type(value.workflows) is not VoiceWorkflows
        or not all(
            callable(getattr(value.workflows, key)) for key in ("remember", "resolve", "replace")
        )
    ):
        raise unavailable()
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _constant(value):
    raise ValueError


async def body(request: Request, binding: VoiceRouteBindings = Depends(bindings)):
    # Existing nurse/Auth/Origin/CSRF dependencies run before JSON parsing.
    # VoiceBodyLimit bounds the ASGI stream even without Content-Length.
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
        or request.headers.get("content-encoding", "identity").lower() != "identity"
        or len(request.headers.getlist("content-type")) != 1
    ):
        raise HTTPException(415, "An unencoded application/json body is required")
    try:
        value = json.loads(
            await request.body(), object_pairs_hook=_object, parse_constant=_constant
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid voice request") from None


def validate(model, value, *, capabilities=None):
    try:
        return model.model_validate(value, context={"voice_capabilities": capabilities})
    except (ValueError, TypeError):
        # Never expose submitted values, including unknown JSON field names.
        raise HTTPException(422, "Invalid voice request") from None


def invoke(operation):
    try:
        return operation()
    except VoiceServiceError as error:
        # Whitelist through a newly constructed error, not mutable provider attributes.
        safe = VoiceServiceError(error.code if type(error.code) is str else "provider_failed")
        raise HTTPException(safe.status_code, safe.detail) from None
    except Exception:
        raise unavailable() from None


def callback(operation):
    def checked():
        if operation() is not None:
            raise ValueError

    invoke(checked)


@router.post(ROOT + "/transcriptions", response_model=TranscriptDraft)
def transcribe(
    patient_id: UUID,
    visit_id: UUID,
    request: Request,
    principal: Principal = Depends(nurse),
    binding: VoiceRouteBindings = Depends(bindings),
    payload: dict = Depends(body),
):
    parsed = validate(TranscriptionBody, payload)
    metadata = validate(
        SpeechToTextRequest,
        dict(
            request_id=request.state.request_id,
            patient_id=patient_id,
            visit_id=visit_id,
            language=parsed.language,
            media_type=parsed.media_type,
            byte_count=parsed.byte_count,
            duration_ms=parsed.duration_ms,
        ),
        capabilities=binding.capabilities,
    )
    try:
        audio = base64.b64decode(parsed.audio_base64, validate=True)
        if (
            not 1 <= len(audio) <= MAX_AUDIO_BYTES
            or len(audio) != parsed.byte_count
            or base64.b64encode(audio).decode("ascii") != parsed.audio_base64
        ):
            raise ValueError
    except (ValueError, binascii.Error):
        raise HTTPException(422, "Invalid voice request") from None
    handle = invoke(
        lambda: binding.service.transcribe(
            principal,
            metadata,
            audio=audio,
            filename=parsed.filename,
        )
    )
    draft = validate(TranscriptDraft, handle.transcript.draft)
    # No durable storage. A missing/unreliable ephemeral lifecycle fails closed.
    callback(lambda: binding.workflows.remember(handle))
    invoke(lambda: binding.service.validate_current_transcript(principal, handle))
    return draft


@router.post(ROOT + "/transcripts/{transcript_id}/review", response_model=ReviewedTranscript)
def review(
    patient_id: UUID,
    visit_id: UUID,
    transcript_id: UUID,
    request: Request,
    principal: Principal = Depends(nurse),
    binding: VoiceRouteBindings = Depends(bindings),
    payload: dict = Depends(body),
):
    parsed = validate(ReviewBody, payload)

    def resolve():
        handle = binding.workflows.resolve(transcript_id)
        if type(handle) is not VoiceTranscriptHandle:
            raise VoiceServiceError("stale")
        draft = handle.transcript.draft
        # Do not allow a handle to retarget another patient/visit/language or session.
        if (
            draft.request_id != transcript_id
            or draft.patient_id != patient_id
            or draft.visit_id != visit_id
            or draft.language != parsed.language
            or handle._authorization.actor_id != principal.id
            or handle._authorization.session_hash != principal.session_hash
        ):
            raise VoiceServiceError("stale")
        return handle

    # Treat malformed lifecycle output like other sanitized backend failures.
    handle = invoke(resolve)
    action = validate(TranscriptReviewAction, parsed.model_dump(exclude={"language"}))
    updated = invoke(lambda: binding.service.review(principal, handle, action))
    result = validate(ReviewedTranscript, updated.transcript)
    callback(lambda: binding.workflows.replace(handle, updated))
    invoke(lambda: binding.service.validate_current_transcript(principal, updated))
    return result


@router.post(ROOT + "/synthesis", response_class=Response)
def synthesize(
    patient_id: UUID,
    visit_id: UUID,
    request: Request,
    principal: Principal = Depends(nurse),
    binding: VoiceRouteBindings = Depends(bindings),
    payload: dict = Depends(body),
):
    parsed = validate(SynthesisBody, payload)
    metadata = validate(
        TextToSpeechRequest,
        dict(
            request_id=request.state.request_id,
            patient_id=patient_id,
            visit_id=visit_id,
            language=parsed.language,
            media_type=parsed.media_type,
            text=parsed.text,
        ),
        capabilities=binding.capabilities,
    )
    result = invoke(lambda: binding.service.synthesize(principal, metadata))
    # Attachment only; synthesis never attests consent/permission to play audio.
    return Response(
        result.audio,
        media_type=result.metadata.media_type,
        headers={
            "Content-Disposition": 'attachment; filename="voice-audio.bin"',
            "X-Voice-Playback-Authorized": "false",
            "X-Voice-Request-ID": str(result.metadata.request_id),
        },
    )
