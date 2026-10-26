"""Offline local-model eligibility only. No inference, installation or activation.

Model names are strict internal aliases, not Ollama wire tags or endpoints. An
artifact digest identifies an exact reviewed artifact; this module does not fetch
or verify model files, reviewer evidence or clinical suitability. No Qwen profile
or language capability is registered. Eligibility is not clinical authorization.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.integrations.ai_capabilities import AICapability, Modality, Operation
from app.schemas.ai import AIContract, Digest, Identifier
from app.schemas.patients import Language


class _Profile(AIContract):
    operation: Operation
    language: Language
    modality: Modality
    model: Identifier
    artifact_digest: Digest
    target_language: Language | None = None


class OllamaCapability(AICapability):
    """Reviewed eligibility metadata, never proof of approval or permission to run.

    Inherits the Phase 5B unconditional enabled=True rejection, strict validation,
    bounds, translation requirements and operation/modality compatibility checks.
    Documentation and approval references must be verified separately in future.
    """

    artifact_digest: Digest

    @property
    def identity(self) -> _Profile:
        return _Profile.model_validate({key: getattr(self, key) for key in _Profile.model_fields})


class OllamaCapabilityRegistry(AIContract):
    provider: Literal["ollama"] = "ollama"
    declarations: Annotated[tuple[OllamaCapability, ...], Field(max_length=128)] = ()

    @model_validator(mode="after")
    def unambiguous(self):
        identities = [item.identity for item in self.declarations]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate or conflicting local AI capability")
        return self

    def eligible(
        self, *, operation, language, modality, model, artifact_digest, target_language=None
    ) -> OllamaCapability:
        """Exact identity match including artifact; never choose a default profile.

        Different artifacts may coexist for historical identity, but a lookup
        must explicitly specify the digest. No latest-model or language fallback.
        """
        try:
            registry = type(self).model_validate(self)
            identity = _Profile.model_validate(
                dict(
                    operation=operation,
                    language=language,
                    modality=modality,
                    model=model,
                    artifact_digest=artifact_digest,
                    target_language=target_language,
                )
            )
        except (ValueError, TypeError):
            raise ValueError("Local AI capability unavailable") from None
        for item in registry.declarations:
            if item.identity == identity:
                return item
        raise ValueError("Local AI capability unavailable")

    def require_enabled(self, **identity):
        self.eligible(**identity)
        raise ValueError("Offline local AI capability disabled")


OLLAMA_CAPABILITIES = OllamaCapabilityRegistry()
