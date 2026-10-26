"""Offline eligibility declarations only; no model IDs, endpoints or live activation.

Approval references are trusted server configuration, not self-authenticating proof.
A future activation review must verify documentation and human authorization.
"""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from app.schemas.ai import AIContract, Identifier
from app.schemas.patients import Language

Operation = Literal[
    "question_generation",
    "translation",
    "clinical_observations",
    "medical_image_interpretation",
    "report_processing",
]
Modality = Literal["text", "image", "report"]


class AICapability(AIContract):
    operation: Operation
    language: Language
    modality: Modality
    model: Identifier
    documentation_reference: Identifier
    approval_reference: Identifier
    target_language: Language | None = None
    enabled: StrictBool = False

    @model_validator(mode="after")
    def exact_operation(self):
        expected = {
            "question_generation": "text",
            "translation": "text",
            "clinical_observations": "text",
            "medical_image_interpretation": "image",
            "report_processing": "report",
        }
        if self.modality != expected[self.operation]:
            raise ValueError("Unsupported operation modality")
        if (self.operation == "translation") != (self.target_language is not None):
            raise ValueError("Explicit translation target required only for translation")
        if self.enabled:
            # Declarations alone never authorize activation.
            raise ValueError("Offline AI declarations cannot activate capabilities")
        return self


class AICapabilityRegistry(AIContract):
    provider: Identifier
    declarations: Annotated[tuple[AICapability, ...], Field(max_length=128)] = ()

    @model_validator(mode="after")
    def unique_profiles(self):
        keys = [
            (item.operation, item.language, item.modality, item.target_language)
            for item in self.declarations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate or conflicting AI capability")
        return self

    def eligible(self, *, operation, language, modality, target_language=None):
        """Exact reviewed profile only; eligibility is NOT processing permission."""
        registry = type(self).model_validate(self)
        for item in registry.declarations:
            if (item.operation, item.language, item.modality, item.target_language) == (
                operation,
                language,
                modality,
                target_language,
            ):
                return item
        raise ValueError("AI capability unavailable")

    def require_enabled(self, **identity):
        self.eligible(**identity)
        raise ValueError("Offline AI capability disabled")


GEMINI_CAPABILITIES = AICapabilityRegistry(provider="gemini")
