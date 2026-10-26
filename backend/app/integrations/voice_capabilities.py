"""Provider-neutral, immutable eligibility registry and explicit operation gate.

Entries are reviewed backend policy inputs, never client assertions or proof of
clinical consent. Their documentation references must be verified by an operator.
Eligibility alone enables nothing. This module contains no provider support list,
credentials, network, storage, application startup or patient information.
"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.patients import Language
from app.schemas.voice import AudioMediaType, Identifier, VoiceContract

Operation = Literal["stt", "tts"]
ModelIdentifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_.-]{1,64}$")]


class VoiceCapabilityMapping(VoiceContract):
    operation: Operation
    language: Language
    provider_language: Identifier
    media_types: Annotated[frozenset[AudioMediaType], Field(min_length=1, max_length=32)]
    voice: Identifier | None = None
    model: ModelIdentifier | None = None
    documentation_reference: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=200)
    ]

    @model_validator(mode="after")
    def operation_profile(self):
        if self.operation == "stt" and (self.voice is not None or self.model is not None):
            raise ValueError("STT must not infer a TTS profile")
        if self.operation == "tts" and (self.voice is None or self.model is None):
            raise ValueError("Explicit TTS voice and model required")
        return self


class VoiceCapabilityRegistry(VoiceContract):
    provider: Identifier
    mappings: Annotated[tuple[VoiceCapabilityMapping, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def unambiguous(self):
        # One reviewed profile per application language and operation. Do not
        # collapse distinct languages into the same provider identifier.
        keys = [(item.operation, item.language) for item in self.mappings]
        wire_keys = [(item.operation, item.provider_language) for item in self.mappings]
        if len(set(keys)) != len(keys) or len(set(wire_keys)) != len(wire_keys):
            raise ValueError("Duplicate or conflicting speech mapping")
        return self

    def eligible(self, *, operation, language, provider_language, voice=None, model=None):
        """Exact metadata match only, not availability or processing authorization."""
        registry = VoiceCapabilityRegistry.model_validate(self)
        for item in registry.mappings:
            if (item.operation, item.language, item.provider_language, item.voice, item.model) == (
                operation,
                language,
                provider_language,
                voice,
                model,
            ):
                return item
        raise ValueError("Speech mapping is not documented in the trusted registry")

    def require_enabled(self, *, enabled=False, media_type, **identity):
        """An explicit true operation gate AND an exact eligible profile are required."""
        if enabled is not True:
            raise ValueError("Speech capability disabled")
        item = self.eligible(**identity)
        if media_type not in item.media_types:
            raise ValueError("Speech media type unavailable")
        return item
