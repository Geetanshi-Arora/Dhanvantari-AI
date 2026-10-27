"""Deterministic fictional eligibility only; no real artifacts or human approvals."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.integrations import ollama_capabilities
from app.integrations.ai_capabilities import GEMINI_CAPABILITIES, AICapabilityRegistry
from app.integrations.gemini import GeminiAdapter, GeminiError
from app.integrations.ollama_capabilities import (
    OLLAMA_CAPABILITIES,
    OllamaCapability,
    OllamaCapabilityRegistry,
)

LANGUAGES = ("en", "hi", "bn", "ta", "te", "mr", "gu", "pa")
OPERATIONS = (
    ("question_generation", "text"),
    ("translation", "text"),
    ("clinical_observations", "text"),
    ("medical_image_interpretation", "image"),
    ("report_processing", "report"),
)
# Synthetic digest fixtures, not hashes of real downloaded models.
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
# Only the fixed isolated-Python invocation below may bypass the parent Popen guard.
_PYTHON_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network, inference and subprocess operations forbidden")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(httpx.Client, "send", denied)
    monkeypatch.setattr(httpx.AsyncClient, "send", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)


def declaration(**changes):
    return (
        dict(
            operation="question_generation",
            language="en",
            modality="text",
            model="fictional-local-model",
            artifact_digest=DIGEST,
            documentation_reference="fictional-doc-only",
            approval_reference="fictional-reference-not-human-approval",
        )
        | changes
    )


def identity(**changes):
    return (
        dict(
            operation="question_generation",
            language="en",
            modality="text",
            model="fictional-local-model",
            artifact_digest=DIGEST,
            target_language=None,
        )
        | changes
    )


def registry(*, changes=None):
    return OllamaCapabilityRegistry(
        declarations=(OllamaCapability.model_validate(declaration(**(changes or {}))),)
    )


def test_empty_production_registry_has_no_eligible_clinical_models():
    assert OLLAMA_CAPABILITIES.provider == "ollama"
    assert OLLAMA_CAPABILITIES.declarations == ()
    for operation, modality in OPERATIONS:
        for language in LANGUAGES:
            for method in [OLLAMA_CAPABILITIES.eligible, OLLAMA_CAPABILITIES.require_enabled]:
                with pytest.raises(ValueError, match="unavailable"):
                    method(
                        **identity(
                            operation=operation,
                            modality=modality,
                            language=language,
                            target_language="en" if operation == "translation" else None,
                        )
                    )


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("operation,modality", OPERATIONS)
def test_fictional_profiles_are_independent_and_never_enabled(language, operation, modality):
    target = (
        "hi"
        if operation == "translation" and language == "en"
        else ("en" if operation == "translation" else None)
    )
    changes = dict(
        operation=operation, modality=modality, language=language, target_language=target
    )
    local = registry(changes=changes)
    item = local.eligible(**identity(**changes))
    assert item.enabled is False
    assert item.artifact_digest == DIGEST
    with pytest.raises(ValueError, match="disabled"):
        local.require_enabled(**identity(**changes))
    assert OllamaCapabilityRegistry.model_validate_json(local.model_dump_json()) == local


@pytest.mark.parametrize(
    "changes",
    [
        dict(language="fr"),
        dict(language="en-US"),
        dict(language="HI"),
        dict(language=1),
        dict(modality="audio"),
        dict(modality="video"),
        dict(operation="diagnose"),
        dict(operation="prescribe"),
        dict(model=""),
        dict(model="x" * 65),
        dict(model="qwen3:1.7b"),
        dict(model="https://not-allowed.invalid"),
        dict(model="../model"),
        dict(model="x\x00y"),
        dict(model=1),
        dict(artifact_digest=""),
        dict(artifact_digest="a" * 63),
        dict(artifact_digest="A" * 64),
        dict(artifact_digest="g" * 64),
        dict(artifact_digest=123),
    ],
)
def test_invalid_declarations_and_lookups_fail_closed(changes):
    with pytest.raises(ValidationError):
        OllamaCapability.model_validate(declaration(**changes))
    with pytest.raises(ValueError, match="unavailable"):
        registry().eligible(**identity(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        dict(language="hi"),
        dict(model="unknown-model"),
        dict(artifact_digest=OTHER_DIGEST),
        dict(operation="clinical_observations"),
        dict(modality="image"),
        dict(target_language="hi"),
    ],
)
def test_no_fallback_for_well_formed_but_ineligible_identity(changes):
    with pytest.raises(ValueError, match="unavailable"):
        registry().eligible(**identity(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        dict(enabled=True),
        dict(enabled=1),
        dict(enabled="false"),
        dict(clinically_approved=True),
        dict(documentation_reference=""),
        dict(approval_reference=""),
        dict(endpoint="forbidden"),
        dict(modality="image"),
        dict(operation="translation"),
        dict(target_language="hi"),
    ],
)
def test_configuration_cannot_activate_or_weaken_phase5b(changes):
    with pytest.raises(ValidationError):
        OllamaCapability.model_validate(declaration(**changes))


@pytest.mark.parametrize(
    "extra",
    [dict(enabled=True), dict(provider="gemini"), dict(endpoint="forbidden"), dict(approved=True)],
)
def test_registry_rejects_provider_switch_or_activation_flags(extra):
    with pytest.raises(ValidationError):
        OllamaCapabilityRegistry.model_validate(extra)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        dict(documentation_reference="different-fictional-doc"),
        dict(approval_reference="different-fictional-reference"),
    ],
)
def test_duplicate_and_conflicting_declarations(changes):
    first = OllamaCapability.model_validate(declaration())
    second = OllamaCapability.model_validate(declaration(**changes))
    with pytest.raises(ValidationError, match="Duplicate or conflicting"):
        OllamaCapabilityRegistry(declarations=(first, second))


def test_exact_artifact_and_model_are_part_of_identity():
    profiles = tuple(
        OllamaCapability.model_validate(declaration(**changes))
        for changes in (
            {},
            dict(artifact_digest=OTHER_DIGEST),
            dict(model="other-fictional-model"),
        )
    )
    local = OllamaCapabilityRegistry(declarations=profiles)
    assert local.eligible(**identity()) == profiles[0]
    assert local.eligible(**identity(artifact_digest=OTHER_DIGEST)) == profiles[1]
    assert local.eligible(**identity(model="other-fictional-model")) == profiles[2]
    with pytest.raises(TypeError):
        local.eligible(operation="question_generation", language="en", modality="text")


def test_translation_target_requires_exact_matching():
    local = registry(changes=dict(operation="translation", target_language="hi"))
    assert (
        local.eligible(**identity(operation="translation", target_language="hi")).enabled is False
    )
    with pytest.raises(ValueError):
        local.eligible(**identity(operation="translation", target_language="bn"))


def test_models_and_catalogue_are_immutable_bounded_and_revalidated():
    item = OllamaCapability.model_validate(declaration())
    local = OllamaCapabilityRegistry(declarations=(item,))
    for record, field, value in [(item, "enabled", True), (local, "declarations", ())]:
        with pytest.raises(ValidationError):
            setattr(record, field, value)
    with pytest.raises(ValidationError):
        OllamaCapabilityRegistry(declarations=(item,) * 129)
    # Unchecked Pydantic copies are not trusted at the lookup boundary.
    forged = local.model_copy(update={"declarations": (item.model_copy(update={"enabled": True}),)})
    with pytest.raises(ValueError, match="unavailable"):
        forged.eligible(**identity())


def test_import_has_no_configuration_or_external_side_effects(monkeypatch):
    before = GEMINI_CAPABILITIES.model_dump_json()
    registry_class = ollama_capabilities.OllamaCapabilityRegistry
    capability_class = ollama_capabilities.OllamaCapability
    catalogue = ollama_capabilities.OLLAMA_CAPABILITIES
    denied_popen = subprocess.Popen
    # Reloads belong to a fresh interpreter, never pytest's shared module graph.
    script = """
import sys
import socket
import subprocess
import os
import importlib
sys.path.insert(0, sys.argv[1])

def denied(*args, **kwargs):
    raise AssertionError("Network, inference and subprocess operations forbidden")

socket.create_connection = denied
socket.socket.connect = denied
socket.getaddrinfo = denied
subprocess.Popen = denied
import httpx
httpx.Client.send = denied
httpx.AsyncClient.send = denied
from app.integrations.ai_capabilities import GEMINI_CAPABILITIES
from app.integrations import ollama_capabilities
before = GEMINI_CAPABILITIES.model_dump_json()
os.environ.update({
    "OLLAMA_HOST": "https://fictional-remote.invalid",
    "OLLAMA_MODEL": "unreviewed-model",
    "OLLAMA_ENABLED": "true",
})
imported = importlib.reload(ollama_capabilities)
assert imported.OLLAMA_CAPABILITIES.declarations == ()
assert GEMINI_CAPABILITIES.model_dump_json() == before
print("PASS")
"""
    # No inherited credentials, proxy settings, database URLs or Python startup paths.
    child_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    with monkeypatch.context() as launch:
        launch.setattr(subprocess, "Popen", _PYTHON_POPEN)
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(Path(__file__).resolve().parents[1])],
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            close_fds=True,
            check=False,
        )
    assert result.returncode == 0, "Isolated import/reload security assertions failed"
    assert result.stdout == "PASS\n" and result.stderr == ""
    assert subprocess.Popen is denied_popen
    assert ollama_capabilities.OllamaCapabilityRegistry is registry_class
    assert ollama_capabilities.OllamaCapability is capability_class
    assert ollama_capabilities.OLLAMA_CAPABILITIES is catalogue
    assert GEMINI_CAPABILITIES.model_dump_json() == before


def test_existing_gemini_activation_blocker_is_unchanged():
    assert GEMINI_CAPABILITIES == AICapabilityRegistry(provider="gemini")
    assert GEMINI_CAPABILITIES.declarations == ()
    with pytest.raises(GeminiError, match="capability_disabled"):
        GeminiAdapter(enabled=True)
    with pytest.raises(ValueError):
        GEMINI_CAPABILITIES.require_enabled(
            operation="question_generation", language="en", modality="text"
        )
