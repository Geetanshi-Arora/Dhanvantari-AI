"""Offline AI contracts, NOT authentication, consent or clinical decision authority.

Privileged records require exact server-issued bindings in validation context.
Future server code must obtain those bindings from verified, current state; never
build them from HTTP/model claims. Hash membership is not evidence of clinical
truth or approval by itself. No runtime, persistence or provider integration exists.
All limits are defensive application ceilings, not verified provider capabilities.
Never expose ValidationError.errors()/JSON or payload dumps: these can contain
submitted values despite hide_input_in_errors. Future HTTP adapters must sanitize.
Use model_validate/model_validate_json, not unchecked model_copy/model_construct.
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.schemas.patients import Language

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _clean_text(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Invalid AI text field")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Invalid AI text encoding") from None
    return value


Text = Annotated[str, StringConstraints(min_length=1, max_length=8000), AfterValidator(_clean_text)]
ShortText = Annotated[
    str, StringConstraints(min_length=1, max_length=1000), AfterValidator(_clean_text)
]
Revision = Annotated[int, Field(ge=1, le=10000)]
Turn = Annotated[int, Field(ge=1, le=50)]
InputSource = Literal["nurse_entered", "nurse_reviewed_transcript"]


@dataclass(frozen=True)
class AITrustContext:
    """Trusted server-only exact record bindings, empty by default.

    Each entry is (record class name, SHA-256 of its model_dump_json() UTF-8 bytes).
    This is an internal serialization convention, not a signed artifact format.
    No JSON field can supply this context. It does not replace authentication,
    assignment/consent rechecks, durable review receipts or database enforcement.
    """

    issued_records: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self):
        if type(self.issued_records) is not frozenset or any(
            type(entry) is not tuple
            or len(entry) != 2
            or not all(type(value) is str for value in entry)
            for entry in self.issued_records
        ):
            raise ValueError("Invalid trusted AI record bindings")


class AIContract(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str):
            if not value.strip() or "\x00" in value:
                raise ValueError("Invalid AI text field")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("Invalid AI text encoding") from None
        return value


def _issued(record, info: ValidationInfo):
    trusted = (info.context or {}).get("ai_trust")
    binding = (type(record).__name__, sha256(record.model_dump_json().encode()).hexdigest())
    if not isinstance(trusted, AITrustContext) or binding not in trusted.issued_records:
        raise ValueError("Trusted server-issued AI record required")


class AIContext(AIContract):
    patient_id: UUID = Field(repr=False)
    visit_id: UUID = Field(repr=False)
    language: Language
    input_revision: Revision
    authorization_snapshot: UUID = Field(repr=False)
    consent_snapshot: UUID = Field(repr=False)

    @model_validator(mode="after")
    def server_bound(self, info: ValidationInfo):
        _issued(self, info)
        return self


class QuestionReview(AIContract):
    """Server-issued review of exact question bytes; not clinician approval."""

    review_id: UUID
    context: AIContext
    question_id: UUID
    question_revision: Revision
    question_hash: Digest
    nurse_id: UUID = Field(repr=False)
    reviewed_at: AwareDatetime
    decision: Literal["approved", "rejected"]

    @model_validator(mode="after")
    def server_issued(self, info: ValidationInfo):
        _issued(self, info)
        return self


class InputReview(AIContract):
    context: AIContext
    review_id: UUID
    input_id: UUID
    revision: Revision
    content_hash: Digest
    language: Language
    source: InputSource
    nurse_id: UUID = Field(repr=False)
    reviewed_at: AwareDatetime

    @model_validator(mode="after")
    def server_issued(self, info: ValidationInfo):
        _issued(self, info)
        return self


class TranslationDraft(AIContract):
    """Optional unreviewed translation; never replaces verified original text."""

    input_id: UUID
    input_revision: Revision
    original_language: Language
    original_content_hash: Digest
    target_language: Language
    translated_text: Text = Field(repr=False)
    state: Literal["unreviewed"] = "unreviewed"

    @model_validator(mode="after")
    def distinct_language(self):
        if self.target_language == self.original_language:
            raise ValueError("Translation must name a distinct target language")
        return self


class VerifiedClinicalInput(AIContract):
    context: AIContext
    input_id: UUID
    revision: Revision
    original_text: Text = Field(repr=False)
    original_language: Language
    translations: Annotated[tuple[TranslationDraft, ...], Field(max_length=7)] = ()
    source: InputSource
    content_hash: Digest
    review: InputReview
    verification_history: Annotated[tuple[InputReview, ...], Field(min_length=1, max_length=10)]
    status: Literal["nurse_verified"]

    @model_validator(mode="after")
    def consistent_review(self):
        if (
            sha256(self.original_text.encode()).hexdigest() != self.content_hash
            or self.review.context != self.context
            or self.review.source != self.source
            or self.original_language != self.context.language
            or self.revision != self.context.input_revision
            or (
                self.review.input_id,
                self.review.revision,
                self.review.content_hash,
                self.review.language,
            )
            != (self.input_id, self.revision, self.content_hash, self.original_language)
        ):
            raise ValueError("Clinical input review binding mismatch")
        targets = [translation.target_language for translation in self.translations]
        if len(set(targets)) != len(targets) or any(
            (
                translation.input_id,
                translation.input_revision,
                translation.original_language,
                translation.original_content_hash,
            )
            != (self.input_id, self.revision, self.original_language, self.content_hash)
            for translation in self.translations
        ):
            raise ValueError("Translation original binding mismatch")
        if self.verification_history[-1] != self.review:
            raise ValueError("Verification history does not end at current review")
        previous_revision, previous_time = 0, None
        for event in self.verification_history:
            if (
                event.input_id != self.input_id
                or event.context.patient_id != self.context.patient_id
                or event.context.visit_id != self.context.visit_id
                or event.language != self.original_language
                or event.source != self.source
                or event.context.input_revision != event.revision
                or event.revision <= previous_revision
                or event.revision > self.revision
                or (previous_time is not None and event.reviewed_at < previous_time)
            ):
                raise ValueError("Invalid verification history")
            previous_revision, previous_time = event.revision, event.reviewed_at
        return self


class EvidenceReference(AIContract):
    """Authorized source binding, not proof that an AI observation is supported."""

    context: AIContext
    evidence_id: UUID
    revision: Revision
    content_hash: Digest
    kind: Literal["verified_input", "vital", "report", "image"]
    measured_at: AwareDatetime | None = None
    units: (
        Annotated[str, StringConstraints(min_length=1, max_length=64), AfterValidator(_clean_text)]
        | None
    ) = None
    page: Annotated[int, Field(ge=1, le=1000)] | None = None
    # Normalized bounding box x,y,width,height, never unbounded pixel coordinates.
    region: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def authorized_source(self, info: ValidationInfo):
        if self.kind == "vital" and (self.measured_at is None or self.units is None):
            raise ValueError("Vital measurement metadata required")
        if (self.page is not None or self.region is not None) and self.kind not in {
            "report",
            "image",
        }:
            raise ValueError("Invalid evidence location")
        if self.region is not None:
            x, y, width, height = self.region
            if not (
                0 <= x <= 1
                and 0 <= y <= 1
                and 0 < width <= 1
                and 0 < height <= 1
                and x + width <= 1
                and y + height <= 1
            ):
                raise ValueError("Invalid evidence region")
        _issued(self, info)
        return self


Evidence = Annotated[tuple[EvidenceReference, ...], Field(max_length=32)]
References = Annotated[tuple[UUID, ...], Field(min_length=1, max_length=32)]


def _evidence(context, evidence):
    ids = [item.evidence_id for item in evidence]
    if len(set(ids)) != len(ids) or any(item.context != context for item in evidence):
        raise ValueError("Duplicate or mismatched evidence binding")
    return set(ids)


class FollowUpDraft(AIContract):
    context: AIContext
    question_id: UUID
    revision: Revision
    turn: Turn
    question: ShortText = Field(repr=False)
    language: Language
    evidence: Evidence  # Explicit empty tuple permits a missing-information question.
    missing_information_rationale: ShortText = Field(repr=False)
    state: Literal["unreviewed"] = "unreviewed"

    @model_validator(mode="after")
    def bound_question(self):
        _evidence(self.context, self.evidence)
        if self.language != self.context.language:
            raise ValueError("Question language mismatch")
        return self


class VerifiedAnswer(AIContract):
    question: FollowUpDraft
    question_review: QuestionReview
    answer: VerifiedClinicalInput
    turn: Turn

    # Original answer and immutable nurse verification are retained in answer.review.
    @model_validator(mode="after")
    def approved_question(self):
        question, review = self.question, self.question_review
        if (
            review.decision != "approved"
            or review.context != question.context
            or self.answer.context != question.context
            or review.question_id != question.question_id
            or review.question_revision != question.revision
            or review.question_hash != sha256(question.question.encode()).hexdigest()
            or self.turn != question.turn
            or self.answer.review.reviewed_at < review.reviewed_at
        ):
            raise ValueError("Answer question review binding mismatch")
        return self


class ObservationDraft(AIContract):
    text: ShortText = Field(repr=False)
    evidence_ids: References
    uncertainty: ShortText = Field(repr=False)
    state: Literal["ai_unverified"] = "ai_unverified"

    @model_validator(mode="after")
    def unique_references(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Duplicate observation evidence")
        return self


class EnglishSummaryDraft(AIContract):
    # Metadata does not detect language or prove translation accuracy. A future
    # trusted language check and nurse/clinician review are still mandatory.
    language: Literal["en"] = "en"
    text: Text = Field(repr=False)
    state: Literal["unreviewed"] = "unreviewed"


class AssessmentDraft(AIContract):
    context: AIContext
    evidence: Evidence
    observations: Annotated[tuple[ObservationDraft, ...], Field(max_length=20)]
    missing_information: Annotated[tuple[ShortText, ...], Field(max_length=20)] = Field(repr=False)
    contradictions: Annotated[tuple[ShortText, ...], Field(max_length=20)] = Field(repr=False)
    limitations: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=20)] = Field(
        repr=False
    )
    doctor_summary: EnglishSummaryDraft = Field(repr=False)
    state: Literal["ai_unverified"] = "ai_unverified"

    @model_validator(mode="after")
    def referenced_evidence(self):
        authorized = _evidence(self.context, self.evidence)
        if any(not set(item.evidence_ids) <= authorized for item in self.observations):
            raise ValueError("Unknown observation evidence")
        return self


class TriageResult(AIContract):
    """Trusted rule-engine output only; never parse model output as this type."""

    context: AIContext
    rules_version: Identifier
    input_revision: Revision
    triggered_rules: Annotated[tuple[Identifier, ...], Field(max_length=32)]
    outcome: Literal["no_rule_triggered", "clinician_review", "urgent_referral"]
    authority: Literal["server_rules"]

    @model_validator(mode="after")
    def independent_rules(self, info: ValidationInfo):
        if (
            self.input_revision != self.context.input_revision
            or len(set(self.triggered_rules)) != len(self.triggered_rules)
            or (self.outcome == "no_rule_triggered") != (not self.triggered_rules)
        ):
            raise ValueError("Invalid triage rule binding")
        _issued(self, info)
        return self
