"""Synthetic dataset integrity only: no inference, clinical or translation scoring.

Source strings/references were authored for tests, not obtained from patients.
A fixed digest forces explicit review of any fixture edit. Neither a digest,
annotation agreement nor pattern matching establishes translation quality,
absence of identifying information or clinical safety. Qualified bilingual and
clinical review remains pending. This file is not a benchmark runner.
"""

import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, get_args

import pytest
from pydantic import Field, ValidationError, model_validator

from app.integrations import gemini, ollama
from app.integrations.ai_capabilities import GEMINI_CAPABILITIES
from app.integrations.ollama_capabilities import OLLAMA_CAPABILITIES
from app.schemas.ai import AIContract, Digest, ShortText
from app.schemas.patients import Language

FIXTURE = Path(__file__).with_name("fixtures") / "ai_multilingual_cases.json"
REVIEWED_FIXTURE_DIGEST = "354f7c9474e9f1568ba405521e818bc7b9e13c830db01dd5a4a67f6bf3965067"
Category = Literal[
    "simple_symptom",
    "negation",
    "duration_chronology",
    "severity",
    "numeric_units",
    "missing_uncertain",
    "code_switching",
    "potentially_urgent_for_review",
    "ambiguous_incomplete",
    "misleading_instruction",
]
FactName = Literal[
    "headache",
    "cough",
    "fever",
    "temperature_timing",
    "chest_pain_and_breathing_difficulty",
    "fatigue",
]
Unknown = Literal["duration", "temperature", "exact_onset_time", "current_temperature", "severity"]
FACT_VALUES = {
    "headache": "present_mild",
    "cough": "absent",
    "fever": "present",
    "temperature_timing": "yesterday",
    "chest_pain_and_breathing_difficulty": "reported",
    "fatigue": "present",
}
EXPECTED_UNKNOWNS = (
    ("duration", "temperature"),
    ("exact_onset_time", "current_temperature"),
    ("exact_onset_time", "severity", "duration"),
    ("duration", "severity"),
)


class StatedFact(AIContract):
    name: FactName
    value: ShortText
    original_excerpt: ShortText
    reference_excerpt: ShortText

    @model_validator(mode="after")
    def no_derived_claims(self):
        if self.value != FACT_VALUES[self.name]:
            raise ValueError("Unexpected synthetic annotation")
        return self


class Number(AIContract):
    kind: Literal["duration", "temperature"]
    value: ShortText
    unit: Literal["days", "degC"]
    original_value: ShortText
    original_unit: ShortText
    reference_value: ShortText
    reference_unit: ShortText

    @model_validator(mode="after")
    def exact_values(self):
        expected = ("2", "days") if self.kind == "duration" else ("38.2", "degC")
        if (self.value, self.unit) != expected or not (
            self.original_value == self.value == self.reference_value
        ):
            raise ValueError("Changed number or unit annotation")
        if self.kind == "temperature" and (
            self.original_unit != "°C" or self.reference_unit != "°C"
        ):
            raise ValueError("Changed temperature unit")
        if self.kind == "duration" and self.reference_unit != "days":
            raise ValueError("Changed duration unit")
        return self


class Case(AIContract):
    case_id: Annotated[str, Field(pattern=r"^fictional-(en|hi|bn|ta|te|mr|gu|pa)-0[1-4]$")]
    language: Language
    categories: Annotated[tuple[Category, ...], Field(min_length=1, max_length=4)]
    original_text: ShortText
    original_sha256: Digest
    english_reference: ShortText
    reference_review: Literal["provisional_not_clinically_verified"]
    stated_facts: Annotated[tuple[StatedFact, ...], Field(min_length=1, max_length=3)]
    unknown_facts: Annotated[tuple[Unknown, ...], Field(min_length=1, max_length=5)]
    numbers: Annotated[tuple[Number, ...], Field(max_length=2)]
    instruction_excerpt: ShortText | None
    clinical_review: Literal["pending_not_triaged"]

    @model_validator(mode="after")
    def mechanical_consistency_only(self):
        if self.case_id.split("-")[1] != self.language:
            raise ValueError("Mismatched language identifier")
        if sha256(self.original_text.encode("utf-8")).hexdigest() != self.original_sha256:
            raise ValueError("Changed original bytes")
        for values in (
            self.categories,
            self.unknown_facts,
            tuple(f.name for f in self.stated_facts),
        ):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate annotations")
        for fact in self.stated_facts:
            if (
                fact.original_excerpt not in self.original_text
                or fact.reference_excerpt not in self.english_reference
            ):
                raise ValueError("Missing explicit annotation span")
        for number in self.numbers:
            if (
                number.original_value not in self.original_text
                or number.original_unit not in self.original_text
                or number.reference_value not in self.english_reference
                or number.reference_unit not in self.english_reference
            ):
                raise ValueError("Missing number or unit span")
        if len({n.kind for n in self.numbers}) != len(self.numbers):
            raise ValueError("Duplicate numeric annotations")
        if ("numeric_units" in self.categories) != (len(self.numbers) == 2):
            raise ValueError("Incomplete numeric annotations")
        if "negation" in self.categories and not any(
            f.name == "cough" and f.value == "absent" and f.reference_excerpt == "no cough"
            for f in self.stated_facts
        ):
            raise ValueError("Missing explicit negation annotation")
        if self.unknown_facts != EXPECTED_UNKNOWNS[int(self.case_id[-1]) - 1]:
            raise ValueError("Missing unknown information")
        if "misleading_instruction" in self.categories:
            if (
                self.instruction_excerpt is None
                or self.instruction_excerpt not in self.original_text
                or self.instruction_excerpt not in self.english_reference
            ):
                raise ValueError("Missing quoted adversarial span")
        elif self.instruction_excerpt is not None:
            raise ValueError("Unexpected adversarial annotation")
        return self


class Dataset(AIContract):
    dataset_id: Literal["fictional-multilingual-symptoms-v1"]
    provenance: Literal["authored_synthetic_no_patient_source"]
    purpose: Literal["offline_fixture_validation_not_model_evaluation"]
    translation_review: Literal["qualified_bilingual_review_required"]
    clinical_review: Literal["clinician_review_required_no_safety_labels"]
    limitations: Annotated[tuple[ShortText, ...], Field(min_length=5, max_length=5)]
    cases: Annotated[tuple[Case, ...], Field(min_length=32, max_length=32)]

    @model_validator(mode="after")
    def unique_case_ids(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate fictional identifiers")
        return self


def load():
    # Fixed path only; no directory discovery, exec, dynamic imports or provider calls.
    return Dataset.model_validate_json(FIXTURE.read_bytes())


def raw_case(index=0):
    return json.loads(FIXTURE.read_bytes())["cases"][index]


def validate_case(value):
    return Case.model_validate_json(json.dumps(value, ensure_ascii=False))


def test_reviewed_fixture_bytes_and_strict_serialization_round_trip():
    assert sha256(FIXTURE.read_bytes()).hexdigest() == REVIEWED_FIXTURE_DIGEST
    dataset = load()
    assert Dataset.model_validate_json(dataset.model_dump_json()) == dataset
    raw = json.loads(FIXTURE.read_bytes())
    for original, case in zip(raw["cases"], dataset.cases, strict=True):
        assert case.original_text.encode("utf-8") == original["original_text"].encode("utf-8")
        assert case.english_reference == original["english_reference"]


@pytest.mark.parametrize("language", get_args(Language))
def test_each_language_has_all_ten_categories(language):
    cases = [c for c in load().cases if c.language == language]
    assert len(cases) == 4
    assert {category for c in cases for category in c.categories} == set(get_args(Category))
    assert len({c.case_id for c in cases}) == 4


@pytest.mark.parametrize("index", range(32))
def test_each_case_is_provisional_with_no_approval_or_derived_decisions(index):
    case = load().cases[index]
    assert case.reference_review == "provisional_not_clinically_verified"
    assert case.clinical_review == "pending_not_triaged"
    assert case.unknown_facts == EXPECTED_UNKNOWNS[index % 4]
    # Closed field vocabulary excludes diagnostic/prescription/identity/approval records.
    # This is NOT a detector for real patient content; provenance required manual review.
    assert {f.name for f in case.stated_facts} <= set(FACT_VALUES)
    assert all(f.value == FACT_VALUES[f.name] for f in case.stated_facts)


@pytest.mark.parametrize("language", get_args(Language))
def test_exact_negation_numeric_duration_and_unknown_annotations(language):
    cases = [c for c in load().cases if c.language == language]
    negation = next(f for f in cases[0].stated_facts if f.name == "cough")
    assert negation.value == "absent"
    assert negation.original_excerpt in cases[0].original_text
    assert negation.reference_excerpt == "no cough"
    assert [(n.kind, n.value, n.unit) for n in cases[1].numbers] == [
        ("duration", "2", "days"),
        ("temperature", "38.2", "degC"),
    ]
    assert "current_temperature" in cases[1].unknown_facts
    assert "Yesterday" in cases[1].english_reference
    assert "exact_onset_time" in cases[2].unknown_facts
    assert cases[3].instruction_excerpt in cases[3].original_text
    assert "not authorization" in cases[3].english_reference


@pytest.mark.parametrize("field", tuple(Case.model_fields))
def test_every_case_field_is_required(field):
    data = raw_case()
    del data[field]
    with pytest.raises(ValidationError):
        validate_case(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("language", "fr"),
        ("language", 1),
        ("case_id", "real-001"),
        ("original_text", 5),
        ("original_text", ""),
        ("original_text", "x" * 1001),
        ("original_text", "x\u0000y"),
        ("reference_review", "approved"),
        ("clinical_review", "safe"),
        ("english_reference", None),
        ("categories", ["diagnosis"]),
        ("categories", ["negation", "negation"]),
        ("unknown_facts", []),
        ("numbers", "none"),
        ("stated_facts", []),
    ],
)
def test_invalid_case_types_bounds_and_claims_fail_closed(field, value):
    with pytest.raises(ValidationError):
        validate_case(raw_case() | {field: value})


@pytest.mark.parametrize("change", ["negation", "duration", "unit", "number", "unknown", "hash"])
def test_changed_annotations_or_originals_are_rejected(change):
    data = raw_case(1 if change in {"duration", "unit", "number", "unknown"} else 0)
    if change == "negation":
        data["stated_facts"][1]["value"] = "present"
    elif change == "duration":
        data["numbers"][0]["value"] = "3"
    elif change == "unit":
        data["numbers"][1]["original_unit"] = "°F"
    elif change == "number":
        data["numbers"][1]["reference_value"] = "39.2"
    elif change == "unknown":
        data["unknown_facts"] = ["exact_onset_time"]
    else:
        data["original_text"] += " Added fictional fact."
    with pytest.raises(ValidationError):
        validate_case(data)


@pytest.mark.parametrize(
    "field",
    [
        "diagnosis",
        "prescription",
        "triage",
        "nurse_approved",
        "patient_id",
        "api_key",
        "enabled",
        "endpoint",
    ],
)
def test_unknown_fields_cannot_add_decisions_identity_credentials_or_activation(field):
    with pytest.raises(ValidationError):
        validate_case(raw_case() | {field: "fictional-forbidden-field"})


def test_duplicate_ids_and_new_nested_fields_are_rejected():
    data = json.loads(FIXTURE.read_bytes())
    data["cases"][1] = data["cases"][0]
    with pytest.raises(ValidationError):
        Dataset.model_validate_json(json.dumps(data))
    data = raw_case()
    data["stated_facts"][0]["diagnosis"] = "fictional-forbidden-field"
    with pytest.raises(ValidationError):
        validate_case(data)


def test_dataset_metadata_cannot_claim_real_data_or_model_evaluation():
    data = json.loads(FIXTURE.read_bytes())
    for key in ("provenance", "purpose", "translation_review", "clinical_review"):
        with pytest.raises(ValidationError):
            Dataset.model_validate_json(json.dumps(data | {key: "approved"}))
    assert "not translation accuracy" in load().limitations[1]


def test_fixture_loading_is_immutable_and_cannot_activate_any_provider(monkeypatch):
    import socket

    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("Fixture validation must never invoke networking")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    for _ in range(2):
        dataset = load()
        assert GEMINI_CAPABILITIES.declarations == OLLAMA_CAPABILITIES.declarations == ()
        for adapter, error in (
            (gemini.GeminiAdapter, gemini.GeminiError),
            (ollama.OllamaAdapter, ollama.OllamaError),
        ):
            with pytest.raises(error, match="capability_disabled"):
                adapter()
            instance = object.__new__(adapter)
            for method in (instance.assess, instance.follow_up):
                with pytest.raises(error, match="capability_disabled"):
                    method()
        with pytest.raises(ValidationError):
            dataset.cases[0].language = "hi"
        with pytest.raises(ValidationError):
            dataset.purpose = "model_evaluation"
