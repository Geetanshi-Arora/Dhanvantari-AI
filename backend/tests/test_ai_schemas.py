"""Fictional offline AI contracts. Issued bindings simulate trusted server records.

No signatures, real nurse approval, consent, clinical truth or live authorization
are claimed. model_construct below ONLY bootstraps fictional issuer fixtures.
"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.ai import (
    AIContext,
    AITrustContext,
    AssessmentDraft,
    EnglishSummaryDraft,
    EvidenceReference,
    FollowUpDraft,
    InputReview,
    ObservationDraft,
    QuestionReview,
    TranslationDraft,
    TriageResult,
    VerifiedAnswer,
    VerifiedClinicalInput,
)

UID = UUID("10000000-0000-4000-8000-000000000001")
OTHER = UUID("10000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 1, 2, tzinfo=UTC)
TEXT = "FICTIONAL INPUT ONLY"
QUESTION = "FICTIONAL QUESTION ONLY"
LANGUAGES = ("en", "hi", "bn", "ta", "te", "mr", "gu", "pa")


@pytest.fixture
def issuer():
    bindings = set()

    def context():
        return {"ai_trust": AITrustContext(frozenset(bindings))}

    def issue(model, **data):
        record = model.model_construct(**data)
        bindings.add((model.__name__, sha256(record.model_dump_json().encode()).hexdigest()))
        return model.model_validate(data, context=context())

    return issue, context


@pytest.fixture
def records(issuer):
    issue, trust = issuer
    ctx = issue(
        AIContext,
        patient_id=UID,
        visit_id=UID,
        language="en",
        input_revision=1,
        authorization_snapshot=UID,
        consent_snapshot=UID,
    )
    review = issue(
        InputReview,
        context=ctx,
        review_id=UID,
        input_id=UID,
        revision=1,
        source="nurse_entered",
        content_hash=sha256(TEXT.encode()).hexdigest(),
        language="en",
        nurse_id=UID,
        reviewed_at=NOW,
    )
    evidence = issue(
        EvidenceReference,
        context=ctx,
        evidence_id=UID,
        revision=1,
        content_hash=review.content_hash,
        kind="verified_input",
    )
    clinical = VerifiedClinicalInput.model_validate(
        dict(
            context=ctx,
            input_id=UID,
            revision=1,
            original_text=TEXT,
            original_language="en",
            source="nurse_entered",
            content_hash=review.content_hash,
            review=review,
            verification_history=(review,),
            status="nurse_verified",
        ),
        context=trust(),
    )
    question = FollowUpDraft.model_validate(
        dict(
            context=ctx,
            question_id=UID,
            revision=1,
            turn=1,
            question=QUESTION,
            language="en",
            evidence=(evidence,),
            missing_information_rationale="FICTIONAL MISSING DATA",
        ),
        context=trust(),
    )
    question_review = issue(
        QuestionReview,
        context=ctx,
        review_id=OTHER,
        question_id=UID,
        question_revision=1,
        question_hash=sha256(QUESTION.encode()).hexdigest(),
        nurse_id=UID,
        reviewed_at=NOW,
        decision="approved",
    )
    assessment = AssessmentDraft.model_validate(
        dict(
            context=ctx,
            evidence=(evidence,),
            observations=(
                ObservationDraft(
                    text="FICTIONAL OBSERVATION",
                    evidence_ids=(UID,),
                    uncertainty="Not clinically verified",
                ),
            ),
            missing_information=(),
            contradictions=(),
            limitations=("FICTIONAL ONLY",),
            doctor_summary=EnglishSummaryDraft(text="FICTIONAL ENGLISH DRAFT"),
        ),
        context=trust(),
    )
    triage = issue(
        TriageResult,
        context=ctx,
        rules_version="fictional-test-1",
        input_revision=1,
        triggered_rules=(),
        outcome="no_rule_triggered",
        authority="server_rules",
    )
    return dict(
        context=ctx,
        review=review,
        evidence=evidence,
        clinical=clinical,
        question=question,
        question_review=question_review,
        assessment=assessment,
        triage=triage,
    )


def change(record, trust, **updates):
    return type(record).model_validate(record.model_dump() | updates, context=trust())


@pytest.mark.parametrize(
    "key",
    [
        "context",
        "review",
        "evidence",
        "clinical",
        "question",
        "question_review",
        "assessment",
        "triage",
    ],
)
def test_valid_roundtrip_and_immutability(records, issuer, key):
    _, trust = issuer
    record = records[key]
    assert type(record).model_validate_json(record.model_dump_json(), context=trust()) == record
    with pytest.raises(ValidationError):
        record.any_unknown_field = "forbidden"
    with pytest.raises(ValidationError):
        change(record, trust, clinician_approved=True)


@pytest.mark.parametrize("key", ["context", "review", "evidence", "question_review", "triage"])
def test_privileged_records_fail_without_server_context(records, key):
    record = records[key]
    with pytest.raises(ValidationError, match="Trusted server-issued"):
        type(record).model_validate_json(record.model_dump_json())


@pytest.mark.parametrize(
    "key,field,value",
    [
        ("context", "input_revision", 2),
        ("context", "patient_id", OTHER),
        ("context", "consent_snapshot", OTHER),
        ("review", "nurse_id", OTHER),
        ("evidence", "evidence_id", OTHER),
        ("evidence", "content_hash", "0" * 64),
        ("question_review", "question_revision", 2),
        ("triage", "rules_version", "forged"),
    ],
)
def test_forged_issued_records_rejected(records, issuer, key, field, value):
    with pytest.raises(ValidationError):
        change(records[key], issuer[1], **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 2),
        ("content_hash", "0" * 64),
        ("original_text", "ALTERED FICTIONAL TEXT"),
        ("status", "unverified"),
        ("status", "clinician_approved"),
        ("original_language", "hi"),
        ("source", "model"),
        ("source", "nurse_reviewed_transcript"),
        ("verification_history", ()),
    ],
)
def test_input_review_gating(records, issuer, field, value):
    with pytest.raises(ValidationError):
        change(records["clinical"], issuer[1], **{field: value})


@pytest.mark.parametrize("value", ["", " ", "x\x00y", "\ud800", "x" * 8001, 123, True])
def test_invalid_clinical_text_is_sanitized(records, issuer, value):
    with pytest.raises(ValidationError) as error:
        change(records["clinical"], issuer[1], original_text=value)
    assert "input_value=" not in str(error.value)
    assert TEXT not in str(error.value)


@pytest.mark.parametrize("language", LANGUAGES)
def test_all_application_languages(issuer, language):
    issue, _ = issuer
    context = issue(
        AIContext,
        patient_id=UID,
        visit_id=UID,
        language=language,
        input_revision=1,
        authorization_snapshot=UID,
        consent_snapshot=UID,
    )
    assert context.language == language


@pytest.mark.parametrize("language", ["fr", "en-US", "HI", "", None])
def test_no_language_fallback(records, issuer, language):
    with pytest.raises(ValidationError):
        change(records["question"], issuer[1], language=language)


@pytest.mark.parametrize(
    "field,value",
    [
        ("turn", 0),
        ("turn", 51),
        ("turn", True),
        ("turn", "1"),
        ("question", "x" * 1001),
        ("evidence", (None,)),
        ("state", "approved"),
        ("evidence", ("fabricated",)),
    ],
)
def test_question_limits(records, issuer, field, value):
    with pytest.raises(ValidationError):
        change(records["question"], issuer[1], **{field: value})


def test_explicit_evidence_required_and_duplicates_rejected(records, issuer):
    record = records["question"]
    data = record.model_dump()
    del data["evidence"]
    with pytest.raises(ValidationError):
        FollowUpDraft.model_validate(data, context=issuer[1]())
    with pytest.raises(ValidationError, match="Duplicate"):
        change(record, issuer[1], evidence=(records["evidence"],) * 2)
    assert change(record, issuer[1], evidence=()).evidence == ()
    with pytest.raises(ValidationError):
        change(record, issuer[1], evidence=(records["evidence"],) * 33)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "url"),
        ("page", 0),
        ("region", (0.9, 0.0, 0.5, 0.5)),
        ("region", (float("nan"), 0.0, 0.1, 0.1)),
        ("units", "x" * 65),
        ("kind", "vital"),
        ("page", 1),
    ],
)
def test_evidence_metadata(records, issuer, field, value):
    with pytest.raises(ValidationError):
        change(records["evidence"], issuer[1], **{field: value})


def test_verified_answer_requires_exact_review(records, issuer):
    data = dict(
        question=records["question"],
        question_review=records["question_review"],
        answer=records["clinical"],
        turn=1,
    )
    answer = VerifiedAnswer.model_validate(data, context=issuer[1]())
    assert answer.answer.original_text == TEXT
    for field, value in [("turn", 2), ("question_review", None)]:
        with pytest.raises(ValidationError):
            VerifiedAnswer.model_validate(data | {field: value}, context=issuer[1]())
    issue, trust = issuer
    rejected = issue(
        QuestionReview,
        **(
            records["question_review"].model_dump()
            | {"context": records["context"], "decision": "rejected"}
        ),
    )
    with pytest.raises(ValidationError, match="Answer question"):
        VerifiedAnswer.model_validate(data | {"question_review": rejected}, context=trust())
    modified = change(records["question"], trust, question="DIFFERENT FICTIONAL QUESTION")
    with pytest.raises(ValidationError, match="Answer question"):
        VerifiedAnswer.model_validate(data | {"question": modified}, context=trust())


@pytest.mark.parametrize(
    "field,value",
    [
        ("missing_information", ("x\x00y",)),
        ("contradictions", (" ",)),
        ("limitations", ()),
        ("limitations", ("x",) * 21),
        ("observations", ()),
    ],
)
def test_assessment_limits(records, issuer, field, value):
    if field == "observations":
        assert change(records["assessment"], issuer[1], **{field: value}).observations == ()
    else:
        with pytest.raises(ValidationError):
            change(records["assessment"], issuer[1], **{field: value})


def test_fabricated_observation_references(records, issuer):
    forged = ObservationDraft(text="FICTIONAL", evidence_ids=(OTHER,), uncertainty="Unknown")
    with pytest.raises(ValidationError, match="Unknown observation evidence"):
        change(records["assessment"], issuer[1], observations=(forged,))
    with pytest.raises(ValidationError):
        ObservationDraft(text="FICTIONAL", evidence_ids=(UID, UID), uncertainty="Unknown")
    with pytest.raises(ValidationError):
        ObservationDraft(text="FICTIONAL", evidence_ids=(), uncertainty="Unknown")


@pytest.mark.parametrize(
    "extra",
    [
        {"language": "hi"},
        {"state": "clinician_approved"},
        {"prescription": "forbidden"},
    ],
)
def test_summary_is_english_draft_only(extra):
    with pytest.raises(ValidationError):
        EnglishSummaryDraft.model_validate({"text": "FICTIONAL ONLY"} | extra)


@pytest.mark.parametrize(
    "extra",
    [
        {"diagnosis": "forbidden"},
        {"treatment_authorized": True},
        {"triage": "urgent"},
    ],
)
def test_model_observations_have_no_clinician_authority(records, issuer, extra):
    with pytest.raises(ValidationError):
        change(records["assessment"], issuer[1], **extra)


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_revision", 2),
        ("authority", "gemini"),
        ("triggered_rules", ("rule", "rule")),
        ("outcome", "safe"),
        ("outcome", "urgent_referral"),
    ],
)
def test_rule_authority_is_separate(records, issuer, field, value):
    with pytest.raises(ValidationError):
        change(records["triage"], issuer[1], **{field: value})


def test_json_cannot_supply_trusted_context(records):
    data = json.loads(records["context"].model_dump_json())
    data["ai_trust"] = {"issued_records": []}
    with pytest.raises(ValidationError):
        AIContext.model_validate_json(json.dumps(data))


def test_repr_hides_clinical_text(records):
    assert TEXT not in repr(records["clinical"])
    assert QUESTION not in repr(records["question"])
    assert "FICTIONAL ENGLISH DRAFT" not in repr(records["assessment"])


def test_provenance_cannot_change_under_same_review(records, issuer):
    with pytest.raises(ValidationError, match="review binding"):
        change(records["clinical"], issuer[1], source="nurse_reviewed_transcript")


@pytest.mark.parametrize("case", ["old_authorization", "other_visit", "other_language"])
def test_even_trusted_evidence_must_match_request_snapshot(records, issuer, case):
    issue, trust = issuer
    data = records["context"].model_dump()
    field, value = {
        "old_authorization": ("authorization_snapshot", OTHER),
        "other_visit": ("visit_id", OTHER),
        "other_language": ("language", "hi"),
    }[case]
    context = issue(AIContext, **(data | {field: value}))
    evidence = issue(EvidenceReference, **(records["evidence"].model_dump() | {"context": context}))
    with pytest.raises(ValidationError, match="mismatched evidence"):
        change(records["question"], trust, evidence=(evidence,))


@pytest.mark.parametrize(
    "metadata",
    [
        {"kind": "vital", "units": "mmHg", "measured_at": NOW},
        {"kind": "image", "region": (0.0, 0.0, 1.0, 1.0)},
        {"kind": "report", "page": 1},
    ],
)
def test_authorized_evidence_variants(records, issuer, metadata):
    evidence = issuer[0](
        EvidenceReference,
        **(records["evidence"].model_dump() | {"context": records["context"]} | metadata),
    )
    assert evidence.kind == metadata["kind"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"kind": "vital"},
        {"page": 1},
        {"kind": "image", "region": (0.5, 0.5, 1.0, 1.0)},
    ],
)
def test_source_metadata_checks_are_independent_of_issuance(records, issuer, metadata):
    with pytest.raises(ValidationError):
        issuer[0](
            EvidenceReference,
            **(records["evidence"].model_dump() | {"context": records["context"]} | metadata),
        )


def test_history_and_revision_binding(records, issuer):
    issue, trust = issuer
    old_context = records["context"]
    context = issue(AIContext, **(old_context.model_dump() | {"input_revision": 2}))
    review = issue(
        InputReview,
        **(
            records["review"].model_dump() | {"context": context, "revision": 2, "review_id": OTHER}
        ),
    )
    data = records["clinical"].model_dump() | {
        "context": context,
        "revision": 2,
        "review": review,
        "verification_history": (records["review"], review),
    }
    assert VerifiedClinicalInput.model_validate(data, context=trust()).revision == 2
    for history in [(review, records["review"]), (review, review), (records["review"],)]:
        with pytest.raises(ValidationError):
            VerifiedClinicalInput.model_validate(
                data | {"verification_history": history}, context=trust()
            )


def test_trusted_rule_results_still_require_consistency(records, issuer):
    issue, _ = issuer
    for changes in [
        dict(input_revision=2),
        dict(outcome="urgent_referral"),
        dict(triggered_rules=("fictional", "fictional")),
    ]:
        with pytest.raises(ValidationError):
            issue(
                TriageResult,
                **(records["triage"].model_dump() | {"context": records["context"]} | changes),
            )
    urgent = issue(
        TriageResult,
        **(
            records["triage"].model_dump()
            | {
                "context": records["context"],
                "triggered_rules": ("fictional",),
                "outcome": "urgent_referral",
            }
        ),
    )
    assert urgent.authority == "server_rules"


def test_translation_never_replaces_verified_original(records, issuer):
    data = dict(
        input_id=UID,
        input_revision=1,
        original_language="en",
        original_content_hash=records["clinical"].content_hash,
        target_language="hi",
        translated_text="FICTIONAL TRANSLATION ONLY",
    )
    translation = TranslationDraft.model_validate(data)
    clinical = change(records["clinical"], issuer[1], translations=(translation,))
    assert clinical.original_text == TEXT
    assert clinical.original_language == "en"
    assert clinical.translations[0].state == "unreviewed"
    assert (
        VerifiedClinicalInput.model_validate_json(clinical.model_dump_json(), context=issuer[1]())
        == clinical
    )
    for updates in [
        dict(input_revision=2),
        dict(input_id=OTHER),
        dict(original_content_hash="0" * 64),
    ]:
        forged = TranslationDraft.model_validate(data | updates)
        with pytest.raises(ValidationError, match="Translation original"):
            change(clinical, issuer[1], translations=(forged,))
    with pytest.raises(ValidationError):
        change(clinical, issuer[1], translations=(translation, translation))
    for updates in [
        dict(target_language="en"),
        dict(target_language="fr"),
        dict(state="nurse_verified"),
        dict(translated_text="x" * 8001),
    ]:
        with pytest.raises(ValidationError):
            TranslationDraft.model_validate(data | updates)
