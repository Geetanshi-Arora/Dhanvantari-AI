"""Offline fictional eligibility only; no verified Gemini support or approvals."""

import pytest
from pydantic import ValidationError

from app.integrations.ai_capabilities import (
    GEMINI_CAPABILITIES,
    AICapability,
    AICapabilityRegistry,
)

LANGUAGES = ("en", "hi", "bn", "ta", "te", "mr", "gu", "pa")
OPERATIONS = (
    ("question_generation", "text"),
    ("translation", "text"),
    ("clinical_observations", "text"),
    ("medical_image_interpretation", "image"),
    ("report_processing", "report"),
)


def declaration(**changes):
    return (
        dict(
            operation="question_generation",
            language="en",
            modality="text",
            model="fictional-test-model",
            documentation_reference="fictional-doc-only",
            approval_reference="fictional-approval-only",
        )
        | changes
    )


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("operation,modality", OPERATIONS)
def test_independent_eligible_profiles_stay_disabled(language, operation, modality):
    data = declaration(operation=operation, modality=modality, language=language)
    if operation == "translation":
        data["target_language"] = "en" if language != "en" else "hi"
    item = AICapability.model_validate(data)
    registry = AICapabilityRegistry(provider="fictional", declarations=(item,))
    identity = dict(
        operation=operation,
        language=language,
        modality=modality,
        target_language=item.target_language,
    )
    assert registry.eligible(**identity) == item
    assert item.enabled is False
    with pytest.raises(ValueError, match="disabled"):
        registry.require_enabled(**identity)
    assert AICapabilityRegistry.model_validate_json(registry.model_dump_json()) == registry


@pytest.mark.parametrize("operation,modality", OPERATIONS)
def test_production_defaults_have_no_support(operation, modality):
    assert GEMINI_CAPABILITIES.declarations == ()
    with pytest.raises(ValueError, match="unavailable"):
        GEMINI_CAPABILITIES.require_enabled(operation=operation, modality=modality, language="en")


@pytest.mark.parametrize("language", ["fr", "en-US", "HI", "", None, 1])
def test_invalid_language(language):
    with pytest.raises(ValidationError):
        AICapability.model_validate(declaration(language=language))


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": True},
        {"enabled": 1},
        {"enabled": "false"},
        {"model": ""},
        {"model": "x" * 65},
        {"documentation_reference": ""},
        {"approval_reference": ""},
        {"endpoint": "https://not-allowed.invalid"},
        {"operation": "diagnose"},
        {"modality": "audio"},
        {"modality": "image"},
        {"target_language": "hi"},
        {"operation": "translation"},
        {"model": "x\x00y"},
    ],
)
def test_invalid_or_activating_declaration(changes):
    with pytest.raises(ValidationError):
        AICapability.model_validate(declaration(**changes))


@pytest.mark.parametrize("model", ["fictional-test-model", "different-test-model"])
def test_duplicate_or_conflicting_profiles(model):
    first = AICapability.model_validate(declaration())
    second = AICapability.model_validate(declaration(model=model))
    with pytest.raises(ValidationError, match="Duplicate or conflicting"):
        AICapabilityRegistry(provider="fictional", declarations=(first, second))


@pytest.mark.parametrize(
    "changes",
    [
        {"language": "hi"},
        {"modality": "image"},
        {"operation": "clinical_observations"},
        {"target_language": "en"},
    ],
)
def test_no_language_modality_or_operation_fallback(changes):
    registry = AICapabilityRegistry(
        provider="fictional", declarations=(AICapability.model_validate(declaration()),)
    )
    with pytest.raises(ValueError, match="unavailable"):
        registry.eligible(
            **(dict(operation="question_generation", language="en", modality="text") | changes)
        )


def test_registry_is_immutable_and_bounded():
    with pytest.raises(ValidationError):
        GEMINI_CAPABILITIES.provider = "changed"
    with pytest.raises(ValidationError):
        AICapabilityRegistry(
            provider="fictional", declarations=(AICapability.model_validate(declaration()),) * 129
        )


def test_translation_targets_are_independent():
    item = AICapability.model_validate(
        declaration(operation="translation", target_language="en", language="hi")
    )
    registry = AICapabilityRegistry(provider="fictional", declarations=(item,))
    assert (
        registry.eligible(
            operation="translation", language="hi", modality="text", target_language="en"
        )
        == item
    )
    with pytest.raises(ValueError):
        registry.eligible(
            operation="translation", language="en", modality="text", target_language="hi"
        )
