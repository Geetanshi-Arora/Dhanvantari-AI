"""Disconnected orchestration for fictional patients; no SQL, provider construction or I/O.

Authorize/audit are synchronous trusted SERVER callbacks, not client flags. They
must finish their short transactions before returning. The authorizer must verify
Auth/session state, visit ownership, assignment and separate approved voice rights
and issue stable, non-reused revisions on every relevant change (including ABA).
No existing care/uploads/future_ai decision implies recording or external processing.

Returned transcript handles are process-local server objects, never HTTP input.
Only weak references to current handles are held; audio is never retained. This is
not durable storage, cross-worker coordination, clinical acceptance or an emergency
workflow. Real authorization/audit/persistence bindings require separate review.
"""

import re
import secrets
import threading
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import Field, StrictBool, StringConstraints

from app.core.security import Principal
from app.schemas.voice import (
    GNANI_CAPABILITIES,
    PlaybackMetadata,
    ReviewedTranscript,
    SpeechToTextRequest,
    TextToSpeechRequest,
    TranscriptDraft,
    TranscriptReviewAction,
    VoiceCapabilities,
    VoiceIdentity,
    review_transcript,
    validate_playback_metadata,
    validate_transcript_result,
)

Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class VoiceAuthorization(VoiceIdentity):
    """Explicit fresh authorization result, issued only by a trusted authorizer.

    Revisions are opaque server epochs, not claims that such columns currently
    exist. The real implementation must derive trustworthy change detection.
    TTS text approval is bound to exact UTF-8 bytes and this nurse, not a client flag.
    """

    actor_id: UUID = Field(repr=False)
    session_hash: Digest = Field(repr=False)
    context_revision: UUID = Field(repr=False)
    assignment_revision: UUID = Field(repr=False)
    care_revision: UUID = Field(repr=False)
    voice_revision: UUID = Field(repr=False)
    authenticated: StrictBool = False
    active_nurse: StrictBool = False
    patient_assigned: StrictBool = False
    care_granted: StrictBool = False
    fictional_patient: StrictBool = False
    voice_notice_approved: StrictBool = False
    recording_permitted: StrictBool = False
    external_voice_processing_permitted: StrictBool = False
    synthesis_permitted: StrictBool = False
    tts_text_digest: Digest | None = Field(default=None, repr=False)
    tts_reviewed_by: UUID | None = Field(default=None, repr=False)


class VoiceAuthorizer(Protocol):
    def __call__(
        self,
        *,
        principal: Principal,
        context: VoiceIdentity,
        operation: str,
        text_digest: str | None,
    ) -> VoiceAuthorization: ...


class VoiceAuditor(Protocol):
    def __call__(
        self, *, action: str, outcome: str, request_id: UUID, actor_id: UUID, resource_id: UUID
    ) -> None: ...


class StructuredSTT(Protocol):
    capabilities: VoiceCapabilities

    def transcribe(
        self, *, audio: bytes, filename: str, request: SpeechToTextRequest
    ) -> TranscriptDraft: ...


class SynthesisResult(Protocol):
    audio: bytes
    metadata: PlaybackMetadata


class StructuredTTS(Protocol):
    capabilities: VoiceCapabilities

    def synthesize(self, *, request: TextToSpeechRequest) -> SynthesisResult: ...


class VoiceServiceError(Exception):
    """Fixed public messages, with no provider/callback exception text or payloads."""

    def __init__(self, code):
        errors = {
            "disabled": (503, "Voice service unavailable"),
            "forbidden": (403, "Voice operation not authorized"),
            "authorization_unavailable": (503, "Voice authorization unavailable"),
            "invalid_request": (422, "Invalid voice request"),
            "unsupported": (422, "Speech capability unavailable"),
            "stale": (409, "Voice context changed; result discarded"),
            "audit_unavailable": (503, "Voice audit unavailable"),
            "provider_timeout": (504, "Voice provider timed out"),
            "provider_failed": (503, "Voice provider unavailable"),
            "invalid_response": (503, "Invalid voice provider response"),
            "review_conflict": (409, "Transcript review conflict"),
            "capacity": (503, "Voice service temporarily unavailable"),
        }
        self.code = code if code in errors else "provider_failed"
        self.status_code, self.detail = errors[self.code]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class VoiceTranscriptHandle:
    transcript: ReviewedTranscript = field(repr=False)
    _authorization: VoiceAuthorization = field(repr=False)


@dataclass(frozen=True, slots=True)
class VoiceAudioResult:
    audio: bytes = field(repr=False)
    metadata: PlaybackMetadata

    @property
    def playback_authorized(self):
        return False


class VoiceService:
    def __init__(
        self,
        *,
        authorizer: VoiceAuthorizer | None = None,
        auditor: VoiceAuditor | None = None,
        stt: StructuredSTT | None = None,
        tts: StructuredTTS | None = None,
        capabilities: VoiceCapabilities = GNANI_CAPABILITIES,
        enabled: bool = False,
        clock=None,
    ):
        if type(enabled) is not bool or not isinstance(capabilities, VoiceCapabilities):
            raise VoiceServiceError("invalid_request")
        try:
            self._capabilities = VoiceCapabilities.model_validate(capabilities)
        except ValueError:
            raise VoiceServiceError("invalid_request") from None
        self._enabled, self._closed = enabled, False
        self._authorize_callback, self._audit_callback = authorizer, auditor
        self._stt, self._tts = stt, tts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._inflight = set()
        self._handles = weakref.WeakValueDictionary()

    def _reserve(self, request_id, operation):
        with self._lock:
            if not self._enabled or self._closed:
                raise VoiceServiceError("disabled")
            if request_id in self._inflight:
                raise VoiceServiceError("stale")
            if len(self._inflight) >= 8 or (operation == "stt" and len(self._handles) >= 32):
                raise VoiceServiceError("capacity")
            self._inflight.add(request_id)

    @staticmethod
    def _principal(principal):
        if (
            not isinstance(principal, Principal)
            or principal.role != "nurse"
            or not isinstance(principal.id, UUID)
            or type(principal.session_hash) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", principal.session_hash)
        ):
            raise VoiceServiceError("forbidden")
        # Do not retain display names in server callbacks.
        return Principal(principal.id, "", "nurse", principal.session_hash)

    @staticmethod
    def _context(request):
        try:
            if not isinstance(request, VoiceIdentity):
                raise ValueError
            return VoiceIdentity(
                **{
                    key: getattr(request, key)
                    for key in ("request_id", "patient_id", "visit_id", "language")
                }
            )
        except (ValueError, TypeError):
            raise VoiceServiceError("invalid_request") from None

    def _authorization(self, principal, context, operation, text_digest):
        if not callable(self._authorize_callback):
            raise VoiceServiceError("authorization_unavailable")
        try:
            value = self._authorize_callback(
                principal=principal, context=context, operation=operation, text_digest=text_digest
            )
            if not isinstance(value, VoiceAuthorization):
                raise ValueError
            value = VoiceAuthorization.model_validate(value)
        except Exception:
            raise VoiceServiceError("authorization_unavailable") from None
        if (
            value.actor_id != principal.id
            or not secrets.compare_digest(value.session_hash, principal.session_hash)
            or any(
                getattr(value, key) != getattr(context, key)
                for key in ("request_id", "patient_id", "visit_id", "language")
            )
            or not all(
                (
                    value.authenticated,
                    value.active_nurse,
                    value.patient_assigned,
                    value.care_granted,
                    value.fictional_patient,
                    value.voice_notice_approved,
                    value.external_voice_processing_permitted,
                )
            )
        ):
            raise VoiceServiceError("forbidden")
        if operation in {"stt", "review"} and not value.recording_permitted:
            raise VoiceServiceError("forbidden")
        if operation == "tts" and (
            not value.synthesis_permitted
            or value.tts_text_digest != text_digest
            or value.tts_reviewed_by != principal.id
        ):
            raise VoiceServiceError("forbidden")
        return value

    def _recheck(self, baseline, principal, context, operation, text_digest):
        if self._closed:
            raise VoiceServiceError("stale")
        try:
            current = self._authorization(principal, context, operation, text_digest)
        except VoiceServiceError as error:
            if error.code == "authorization_unavailable":
                raise
            raise VoiceServiceError("stale") from None
        if current != baseline:
            raise VoiceServiceError("stale")

    def _audit(self, operation, outcome, principal, context):
        try:
            if not callable(self._audit_callback):
                raise ValueError
            result = self._audit_callback(
                action="voice." + operation,
                outcome=outcome,
                request_id=context.request_id,
                actor_id=principal.id,
                resource_id=context.patient_id,
            )
            if result is not None:
                raise ValueError
        except Exception:
            raise VoiceServiceError("audit_unavailable") from None

    def _execute(
        self,
        *,
        operation,
        principal,
        context,
        work,
        finalize,
        baseline=None,
        text_digest=None,
        accept=None,
    ):
        self._reserve(context.request_id, operation)
        try:
            before = self._authorization(principal, context, operation, text_digest)
            if baseline is not None and before != baseline:
                raise VoiceServiceError("stale")
            self._audit(operation, "attempt", principal, context)
            self._recheck(before, principal, context, operation, text_digest)
            result = work()
            self._recheck(before, principal, context, operation, text_digest)
            result = finalize(result, before)
            self._audit(operation, "success", principal, context)
            # Audit callbacks must not conceal intervening revocation/context changes.
            self._recheck(before, principal, context, operation, text_digest)
            return accept(result) if accept else result
        except VoiceServiceError as error:
            if error.code != "audit_unavailable":
                self._audit(
                    operation,
                    "stale"
                    if error.code == "stale"
                    else "denied"
                    if error.code == "forbidden"
                    else "failed",
                    principal,
                    context,
                )
            raise
        except Exception:
            self._audit(operation, "failed", principal, context)
            raise VoiceServiceError("provider_failed") from None
        finally:
            with self._lock:
                self._inflight.discard(context.request_id)

    def _provider(self, operation, request):
        provider = self._stt if operation == "stt" else self._tts
        try:
            value = VoiceCapabilities.model_validate(provider.capabilities)
            declared = getattr(value, operation)
            if (
                value.provider != self._capabilities.provider
                or not declared.available
                or request.language not in declared.languages
                or request.media_type not in declared.media_types
            ):
                raise ValueError
        except Exception:
            raise VoiceServiceError("unsupported") from None
        return provider, value

    def _provider_call(self, operation, provider, request, capabilities, **kwargs):
        try:
            result = (
                provider.transcribe(request=request, **kwargs)
                if operation == "stt"
                else provider.synthesize(request=request)
            )
        except Exception as error:
            # Do not import vendor implementations or expose their exception text.
            code = getattr(error, "code", None)
            if isinstance(error, TimeoutError) or code == "timeout":
                raise VoiceServiceError("provider_timeout") from None
            raise VoiceServiceError("provider_failed") from None
        try:
            _, current = self._provider(operation, request)
        except VoiceServiceError:
            raise VoiceServiceError("stale") from None
        if current != capabilities:
            raise VoiceServiceError("stale")
        return result

    def transcribe(
        self, principal, request, *, audio: bytes, filename: str
    ) -> VoiceTranscriptHandle:
        principal = self._principal(principal)
        context = self._context(request)
        try:
            if not isinstance(request, SpeechToTextRequest):
                raise ValueError
            request = SpeechToTextRequest.model_validate(
                request, context={"voice_capabilities": self._capabilities}
            )
            if (
                type(audio) is not bytes
                or not 1 <= len(audio) <= 4 * 1024 * 1024
                or request.byte_count != len(audio)
                or type(filename) is not str
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9]{1,5}", filename)
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise VoiceServiceError("invalid_request") from None

        def work():
            with self._lock:
                if context.request_id in self._handles:
                    raise VoiceServiceError("stale")
            provider, capabilities = self._provider("stt", request)
            return self._provider_call(
                "stt", provider, request, capabilities, audio=audio, filename=filename
            )

        def finalize(raw, authorization):
            try:
                if not isinstance(raw, TranscriptDraft):
                    raise ValueError
                draft = validate_transcript_result(
                    raw, request=request, capabilities=self._capabilities
                )
                return VoiceTranscriptHandle(ReviewedTranscript(draft=draft), authorization)
            except ValueError:
                raise VoiceServiceError("invalid_response") from None

        def accept(result):
            with self._lock:
                if self._closed or context.request_id in self._handles:
                    raise VoiceServiceError("stale")
                self._handles[context.request_id] = result
            return result

        return self._execute(
            operation="stt",
            principal=principal,
            context=context,
            work=work,
            finalize=finalize,
            accept=accept,
        )

    def review(self, principal, handle, action) -> VoiceTranscriptHandle:
        principal = self._principal(principal)
        if type(handle) is not VoiceTranscriptHandle:
            raise VoiceServiceError("stale")
        context = self._context(handle.transcript.draft)

        def current_handle():
            with self._lock:
                if self._handles.get(context.request_id) is not handle:
                    raise VoiceServiceError("stale")

        def work():
            current_handle()
            try:
                if not isinstance(action, TranscriptReviewAction):
                    raise ValueError
                validated_action = TranscriptReviewAction.model_validate(action)
                # Confirmation is separate from explicitly submitting reviewed text.
                # An accurate draft may be submitted unchanged in a correction event.
                if (
                    validated_action.action == "confirm"
                    and handle.transcript.state != "nurse_corrected"
                ):
                    raise ValueError
                return review_transcript(
                    handle.transcript, validated_action, principal=principal, at=self._clock()
                )
            except ValueError:
                raise VoiceServiceError("review_conflict") from None

        def accept(result):
            with self._lock:
                if self._closed or self._handles.get(context.request_id) is not handle:
                    raise VoiceServiceError("stale")
                self._handles[context.request_id] = result
            return result

        return self._execute(
            operation="review",
            principal=principal,
            context=context,
            work=work,
            finalize=lambda transcript, authorization: VoiceTranscriptHandle(
                transcript, authorization
            ),
            baseline=handle._authorization,
            accept=accept,
        )

    def validate_current_transcript(self, principal, handle) -> None:
        """Recheck the acceptance boundary after an ephemeral HTTP lifecycle callback.

        No history changes, durable storage, provider calls or clinical approval.
        Callers must hold the exact server handle, not a deserialized HTTP object.
        """
        principal = self._principal(principal)
        if type(handle) is not VoiceTranscriptHandle:
            raise VoiceServiceError("stale")
        context = self._context(handle.transcript.draft)
        try:
            self._recheck(handle._authorization, principal, context, "review", None)
            with self._lock:
                if self._closed or self._handles.get(context.request_id) is not handle:
                    raise VoiceServiceError("stale")
        except VoiceServiceError:
            self._audit("review", "stale", principal, context)
            raise

    def synthesize(self, principal, request) -> VoiceAudioResult:
        principal = self._principal(principal)
        context = self._context(request)
        try:
            if not isinstance(request, TextToSpeechRequest):
                raise ValueError
            request = TextToSpeechRequest.model_validate(
                request, context={"voice_capabilities": self._capabilities}
            )
            digest = sha256(request.text.encode("utf-8")).hexdigest()
        except (ValueError, TypeError):
            raise VoiceServiceError("invalid_request") from None

        def work():
            provider, capabilities = self._provider("tts", request)
            return self._provider_call("tts", provider, request, capabilities)

        def finalize(raw, authorization):
            try:
                audio = raw.audio
                if type(audio) is not bytes or not 1 <= len(audio) <= 4 * 1024 * 1024:
                    raise ValueError
                metadata = validate_playback_metadata(
                    raw.metadata, request=request, capabilities=self._capabilities
                )
                if metadata.byte_count != len(audio):
                    raise ValueError
                return VoiceAudioResult(audio, metadata)
            except Exception:
                raise VoiceServiceError("invalid_response") from None

        return self._execute(
            operation="tts",
            principal=principal,
            context=context,
            work=work,
            finalize=finalize,
            text_digest=digest,
        )

    def close(self):
        with self._lock:
            self._closed = True
            self._handles.clear()
