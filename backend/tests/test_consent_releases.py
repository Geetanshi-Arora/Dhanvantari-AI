"""Offline NON-APPROVED fictional notices, reviewers and ephemeral test keys only.

No real authority, approvals or production signing material is represented here.
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256

import pytest
from app.schemas.consent_notices import PRODUCTION_NOTICE_CATALOGUE, NoticeArtifact, NoticeIdentity
from app.schemas.consent_releases import (
    PRODUCTION_REVIEWER_REGISTRY,
    ApprovalEvidence,
    ApprovalScope,
    LifecycleTransition,
    ReleaseEntry,
    ReleaseManifest,
    ReleaseTrustPolicy,
    ReviewerRegistry,
    RoleRequirement,
    TrustedKey,
    TrustedReleaseHistory,
    TrustedReviewer,
    artifact_digest,
    parse_document,
)
from app.services.consent_release_validation import (
    OfflineValidationError,
    validate_release,
    verify_evidence,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

NOW = datetime(2026, 1, 2, tzinfo=UTC)
TEXT = "NON-APPROVED FICTIONAL RELEASE TEST CONTENT ONLY.\n"
IDENTITY = NoticeIdentity(purpose="care", notice_version="fixture-only", language="en")
HASH = sha256(TEXT.encode()).hexdigest()
ROLES = ("clinical", "legal")  # Fictional test policy; NOT a production requirement.


def signed(
    keys,
    role,
    *,
    at=NOW,
    release_id="fixture-release-1",
    previous=None,
    action="approve",
    **changes,
):
    record = ApprovalEvidence(
        identity=IDENTITY,
        content_hash=HASH,
        reviewer_id="fictional_" + role,
        scope=ApprovalScope(purpose="care", language="en", role=role, action=action),
        decision="approved",
        decided_at=at,
        expires_at=at + timedelta(days=30),
        release_id=release_id,
        previous_manifest_digest=previous,
        algorithm="Ed25519",
        key_id="fictional_key_" + role,
        signature="0" * 128,
    ).model_copy(update=changes)
    record = ApprovalEvidence.model_validate(record)
    return record.model_copy(update={"signature": keys[role].sign(record.signing_bytes()).hex()})


@pytest.fixture
def bundle():
    keys = {role: Ed25519PrivateKey.generate() for role in ROLES}
    registry = ReviewerRegistry(
        reviewers=tuple(
            TrustedReviewer(
                reviewer_id="fictional_" + role,
                scopes=tuple(
                    ApprovalScope(purpose="care", language="en", role=role, action=action)
                    for action in ("approve", "retire", "reactivate")
                ),
            )
            for role in ROLES
        ),
        keys=tuple(
            TrustedKey(
                key_id="fictional_key_" + role,
                reviewer_id="fictional_" + role,
                algorithm="Ed25519",
                public_key=keys[role].public_key().public_bytes_raw().hex(),
                valid_from=NOW - timedelta(days=1),
                valid_until=NOW + timedelta(days=90),
                revoked=False,
            )
            for role in ROLES
        ),
    )
    policy = ReleaseTrustPolicy(
        requirements=tuple(
            RoleRequirement(purpose="care", action=action, roles=ROLES)
            for action in ("approve", "retire", "reactivate")
        ),
        distinct_reviewers=True,
        allow_genesis=True,
        allow_reactivation=False,
    )
    evidence = tuple(signed(keys, role) for role in ROLES)
    digests = tuple(e.digest() for e in evidence)
    artifact = NoticeArtifact(
        **IDENTITY.model_dump(),
        text=TEXT,
        content_hash=HASH,
        approval_references=digests,
        approved=True,
        approved_at=NOW,
        retired=False,
        retired_at=None,
    )
    entry = ReleaseEntry(
        identity=IDENTITY,
        content_hash=HASH,
        artifact_digest=artifact_digest(artifact),
        evidence_digests=digests,
        state="active",
        state_changed_at=NOW,
    )
    manifest = ReleaseManifest(
        release_id="fixture-release-1",
        created_at=NOW,
        previous_manifest_digest=None,
        entries=(entry,),
        transitions=(
            LifecycleTransition(identity=IDENTITY, action="activate", evidence_digests=digests),
        ),
    )
    return dict(
        manifest=manifest,
        artifacts=(artifact,),
        evidence=evidence,
        registry=registry,
        policy=policy,
        history=TrustedReleaseHistory(head=None, head_digest=None, release_ids=()),
        at=NOW,
    ), keys


def next_release(args, keys, action="retire", release_id="fixture-release-2"):
    at = args["at"] + timedelta(days=1)
    old = args["manifest"]
    previous = old.digest()
    additions = tuple(
        signed(keys, role, at=at, release_id=release_id, previous=previous, action=action)
        for role in ROLES
    )
    refs = tuple(e.digest() for e in additions)
    artifact = NoticeArtifact.model_validate(
        args["artifacts"][0].model_copy(
            update={"retired": action == "retire", "retired_at": at if action == "retire" else None}
        )
    )
    entry = ReleaseEntry.model_validate(
        old.entries[0].model_copy(
            update={
                "state": "retired" if action == "retire" else "active",
                "state_changed_at": at,
                "evidence_digests": old.entries[0].evidence_digests + refs,
            }
        )
    )
    manifest = ReleaseManifest(
        release_id=release_id,
        created_at=at,
        previous_manifest_digest=previous,
        entries=(entry,),
        transitions=(LifecycleTransition(identity=IDENTITY, action=action, evidence_digests=refs),),
    )
    return args | dict(
        manifest=manifest,
        artifacts=(artifact,),
        evidence=args["evidence"] + additions,
        history=TrustedReleaseHistory(
            head=old,
            head_digest=previous,
            release_ids=args["history"].release_ids + (old.release_id,),
        ),
        at=at,
    )


def verify(record, args):
    return verify_evidence(
        record,
        identity=IDENTITY,
        content_hash=HASH,
        action="approve",
        release_id="fixture-release-1",
        previous_manifest_digest=None,
        registry=args["registry"],
        at=args["at"],
    )


def test_valid_signatures_and_release_never_authorize_publication(bundle):
    args, _ = bundle
    result = validate_release(**args)
    assert result.manifest_digest == args["manifest"].digest()
    assert result.checked_entries == 1
    assert not result.publication_authorized and not result.new_decisions_authorized
    assert verify(args["evidence"][0], args) == args["evidence"][0]
    assert PRODUCTION_REVIEWER_REGISTRY.reviewers == ()
    assert PRODUCTION_REVIEWER_REGISTRY.keys == ()
    assert PRODUCTION_NOTICE_CATALOGUE.artifacts == ()


@pytest.mark.parametrize(
    "change",
    [
        "reviewer",
        "key",
        "signature",
        "role",
        "identity",
        "hash",
        "release",
        "previous",
        "rejected",
        "expired",
        "future",
    ],
)
def test_evidence_tampering_or_mismatch_fails_closed(bundle, change):
    args, keys = bundle
    record = args["evidence"][0]
    changes = {
        "reviewer": {"reviewer_id": "unknown"},
        "key": {"key_id": "unknown"},
        "signature": {"signature": "0" * 128},
        "role": {"scope": record.scope.model_copy(update={"role": "untrusted"})},
        "identity": {"identity": IDENTITY.model_copy(update={"notice_version": "different"})},
        "hash": {"content_hash": "0" * 64},
        "release": {"release_id": "different"},
        "previous": {"previous_manifest_digest": "0" * 64},
        "rejected": {"decision": "rejected"},
        "expired": {"decided_at": NOW - timedelta(days=2), "expires_at": NOW},
        "future": {"decided_at": NOW + timedelta(seconds=1)},
    }[change]
    record = record.model_copy(update=changes)
    # Even authentic signatures cannot authorize rejected/expired decisions or mismatched bindings.
    if change in {"rejected", "expired", "future"}:
        record = record.model_copy(
            update={"signature": keys[ROLES[0]].sign(record.signing_bytes()).hex()}
        )
    with pytest.raises(OfflineValidationError):
        verify(record, args)


@pytest.mark.parametrize(
    "changes",
    [
        {"revoked": True},
        {"valid_until": NOW},
        {"valid_from": NOW + timedelta(seconds=1)},
        {"public_key": "0" * 64},
    ],
)
def test_key_lifecycle_and_wrong_key_rejected(bundle, changes):
    args, _ = bundle
    key = args["registry"].keys[0].model_copy(update=changes)
    registry = args["registry"].model_copy(update={"keys": (key, args["registry"].keys[1])})
    with pytest.raises(OfflineValidationError):
        verify(args["evidence"][0], args | {"registry": registry})


def test_same_key_cannot_impersonate_distinct_reviewers(bundle):
    args, _ = bundle
    first, second = args["registry"].keys
    with pytest.raises(ValidationError, match="Reused trusted key"):
        ReviewerRegistry(
            reviewers=args["registry"].reviewers,
            keys=(first, second.model_copy(update={"public_key": first.public_key})),
        )


@pytest.mark.parametrize(
    "change",
    [
        "empty_registry",
        "missing_policy",
        "missing_history",
        "no_genesis",
        "missing_roles",
        "duplicate_evidence",
        "conflicting_evidence",
        "missing_evidence",
        "different_artifact",
        "unsafe_path",
        "no_transition",
    ],
)
def test_release_preconditions_fail_closed(bundle, change):
    args, keys = bundle
    if change == "empty_registry":
        args = args | {"registry": PRODUCTION_REVIEWER_REGISTRY}
    elif change == "missing_policy":
        args = args | {"policy": None}
    elif change == "missing_history":
        args = args | {"history": None}
    elif change == "no_genesis":
        args = args | {"policy": args["policy"].model_copy(update={"allow_genesis": False})}
    elif change == "missing_roles":
        args = args | {
            "policy": args["policy"].model_copy(
                update={
                    "requirements": (
                        RoleRequirement(
                            purpose="care", action="approve", roles=ROLES + ("translation",)
                        ),
                    )
                }
            )
        }
    elif change == "duplicate_evidence":
        args = args | {"evidence": args["evidence"] * 2}
    elif change == "conflicting_evidence":
        args = args | {
            "evidence": args["evidence"] + (signed(keys, ROLES[0], decision="rejected"),)
        }
    elif change == "missing_evidence":
        args = args | {"evidence": args["evidence"][:1]}
    elif change == "different_artifact":
        text = TEXT + "ALTERED"
        args = args | {
            "artifacts": (
                args["artifacts"][0].model_copy(
                    update={"text": text, "content_hash": sha256(text.encode()).hexdigest()}
                ),
            )
        }
    elif change == "unsafe_path":
        args = args | {
            "manifest": args["manifest"].model_copy(update={"release_id": "../../operator"})
        }
    elif change == "no_transition":
        args = args | {"manifest": args["manifest"].model_copy(update={"transitions": ()})}
    with pytest.raises(OfflineValidationError):
        validate_release(**args)


def test_missing_roles_even_with_valid_signatures(bundle):
    args, _ = bundle
    assert all(verify(e, args) for e in args["evidence"])
    policy = args["policy"].model_copy(
        update={
            "requirements": (RoleRequirement(purpose="uploads", action="approve", roles=ROLES),)
        }
    )
    with pytest.raises(OfflineValidationError, match="missing_required_roles"):
        validate_release(**(args | {"policy": policy}))


def test_retirement_preserves_historical_artifacts_evidence_and_content(bundle):
    args, keys = bundle
    validate_release(**args)
    retired = next_release(args, keys)
    assert validate_release(**retired).checked_entries == 1
    assert set(args["manifest"].entries[0].evidence_digests) < set(
        retired["manifest"].entries[0].evidence_digests
    )
    assert retired["artifacts"][0].retired
    assert artifact_digest(args["artifacts"][0]) == artifact_digest(retired["artifacts"][0])


def test_reactivation_requires_explicit_policy_and_new_authorized_evidence(bundle):
    args, keys = bundle
    retired = next_release(args, keys)
    active = next_release(retired, keys, "reactivate", "fixture-release-3")
    with pytest.raises(OfflineValidationError, match="reactivation_not_authorized"):
        validate_release(**active)
    active["policy"] = active["policy"].model_copy(update={"allow_reactivation": True})
    assert not validate_release(**active).publication_authorized


@pytest.mark.parametrize(
    "change",
    [
        "previous",
        "reused_id",
        "changed_text",
        "dropped_history",
        "missing_evidence",
        "no_transition",
        "wrong_action",
        "old_transition_evidence",
    ],
)
def test_cross_release_conflicts_and_unauthorized_lifecycle_rejected(bundle, change):
    args, keys = bundle
    second = next_release(args, keys)
    manifest = second["manifest"]
    if change == "previous":
        manifest = manifest.model_copy(update={"previous_manifest_digest": "0" * 64})
    elif change == "reused_id":
        manifest = manifest.model_copy(update={"release_id": args["manifest"].release_id})
    elif change == "changed_text":
        text = TEXT + "CHANGED CONTENT"
        artifact = second["artifacts"][0].model_copy(
            update={"text": text, "content_hash": sha256(text.encode()).hexdigest()}
        )
        entry = manifest.entries[0].model_copy(
            update={
                "content_hash": artifact.content_hash,
                "artifact_digest": artifact_digest(artifact),
            }
        )
        second = second | {"artifacts": (artifact,)}
        manifest = manifest.model_copy(update={"entries": (entry,)})
    elif change == "dropped_history":
        other = IDENTITY.model_copy(update={"notice_version": "different"})
        manifest = manifest.model_copy(
            update={"entries": (manifest.entries[0].model_copy(update={"identity": other}),)}
        )
    elif change == "missing_evidence":
        second = second | {"evidence": second["evidence"][2:]}
    elif change == "no_transition":
        manifest = manifest.model_copy(update={"transitions": ()})
    elif change == "wrong_action":
        manifest = manifest.model_copy(
            update={
                "transitions": (
                    manifest.transitions[0].model_copy(update={"action": "reactivate"}),
                )
            }
        )
    elif change == "old_transition_evidence":
        manifest = manifest.model_copy(
            update={
                "transitions": (
                    manifest.transitions[0].model_copy(
                        update={"evidence_digests": args["manifest"].entries[0].evidence_digests}
                    ),
                )
            }
        )
    with pytest.raises(OfflineValidationError):
        validate_release(**(second | {"manifest": manifest}))


def test_unchanged_history_can_survive_evidence_expiry_without_reauthorization(bundle):
    args, _ = bundle
    at = NOW + timedelta(days=40)
    old = args["manifest"]
    manifest = ReleaseManifest(
        release_id="fixture-release-2",
        created_at=at,
        previous_manifest_digest=old.digest(),
        entries=old.entries,
        transitions=(),
    )
    history = TrustedReleaseHistory(
        head=old, head_digest=old.digest(), release_ids=(old.release_id,)
    )
    assert validate_release(**(args | {"manifest": manifest, "history": history, "at": at}))
    with pytest.raises(OfflineValidationError):
        verify(args["evidence"][0], args | {"at": at})


@pytest.mark.parametrize(
    "raw",
    [
        b'{"release_id":"a","release_id":"b"}',
        b'{"x":{"a":1,"a":2}}',
        b'{"x":NaN}',
        b"[]",
        b"\xff",
        b"{",
        b"x" * 2_000_001,
    ],
)
def test_malformed_or_ambiguous_json_rejected(raw):
    with pytest.raises(ValueError, match="Invalid offline document"):
        parse_document(ReleaseManifest, raw)


def test_transport_order_does_not_change_canonical_signed_bytes_or_manifest_digest(bundle):
    args, _ = bundle
    record = args["evidence"][0]
    raw = json.dumps(record.model_dump(mode="json"), sort_keys=False, indent=2).encode()
    parsed = parse_document(ApprovalEvidence, raw)
    assert parsed.signing_bytes() == record.signing_bytes()
    assert parsed.digest() == record.digest()
    assert verify(parsed, args)
    manifest = parse_document(ReleaseManifest, args["manifest"].model_dump_json().encode())
    assert manifest.digest() == args["manifest"].digest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("algorithm", "none"),
        ("signature", "bad"),
        ("expires_at", 42),
        ("unexpected_path", "/etc/passwd"),
    ],
)
def test_strict_evidence_fields_rejected(bundle, field, value):
    args, _ = bundle
    with pytest.raises(ValidationError):
        ApprovalEvidence.model_validate(args["evidence"][0].model_dump() | {field: value})


def test_registry_policy_manifest_and_evidence_are_immutable(bundle):
    args, _ = bundle
    for value, field, replacement in [
        (args["manifest"], "release_id", "different"),
        (args["registry"], "keys", ()),
        (args["policy"], "allow_reactivation", True),
        (args["evidence"][0], "decision", "rejected"),
    ]:
        with pytest.raises(ValidationError):
            setattr(value, field, replacement)


def test_no_io_or_application_integration_in_validator():
    import ast
    from pathlib import Path

    source = Path(__file__).parents[1] / "app/services/consent_release_validation.py"
    tree = ast.parse(source.read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(
        (n.module or "").startswith(
            ("sqlalchemy", "app.main", "app.services.patients", "subprocess", "socket")
        )
        for n in imports
    )


@pytest.mark.parametrize("change", ["scope", "reviewer_binding", "key_missing", "reviewer_missing"])
def test_validly_signed_evidence_still_requires_exact_trusted_authority(bundle, change):
    args, keys = bundle
    record = args["evidence"][0]
    registry = args["registry"]
    if change == "scope":
        record = signed(
            keys,
            ROLES[0],
            scope=ApprovalScope(
                purpose="care", language="en", role="translation", action="approve"
            ),
        )
    elif change == "reviewer_binding":
        registry = registry.model_copy(
            update={
                "keys": (
                    registry.keys[0].model_copy(
                        update={"reviewer_id": registry.reviewers[1].reviewer_id}
                    ),
                    registry.keys[1],
                )
            }
        )
    elif change == "key_missing":
        registry = registry.model_copy(update={"keys": registry.keys[1:]})
    else:
        registry = registry.model_copy(update={"reviewers": ()})
    with pytest.raises(OfflineValidationError):
        verify(record, args | {"registry": registry})


@pytest.mark.parametrize("target", ["entries", "transitions", "artifacts"])
def test_duplicate_manifest_or_artifact_identities_rejected(bundle, target):
    args, _ = bundle
    if target == "artifacts":
        with pytest.raises(OfflineValidationError, match="duplicate_artifact"):
            validate_release(**(args | {"artifacts": args["artifacts"] * 2}))
    else:
        manifest = args["manifest"].model_copy(
            update={target: getattr(args["manifest"], target) * 2}
        )
        with pytest.raises(OfflineValidationError, match="invalid_contract"):
            validate_release(**(args | {"manifest": manifest}))


@pytest.mark.parametrize(
    "change",
    ["entry_hash", "artifact_digest", "ledger_digest", "retirement_time", "removed_old_refs"],
)
def test_inconsistent_digests_and_lifecycle_metadata_rejected(bundle, change):
    args, keys = bundle
    second = next_release(args, keys)
    if change == "ledger_digest":
        second["history"] = second["history"].model_copy(update={"head_digest": "0" * 64})
    elif change == "retirement_time":
        second["artifacts"] = (second["artifacts"][0].model_copy(update={"retired_at": NOW}),)
    else:
        entry = second["manifest"].entries[0]
        changes = {
            "entry_hash": {"content_hash": "0" * 64},
            "artifact_digest": {"artifact_digest": "0" * 64},
            "removed_old_refs": {"evidence_digests": entry.evidence_digests[2:]},
        }[change]
        second["manifest"] = second["manifest"].model_copy(
            update={"entries": (entry.model_copy(update=changes),)}
        )
    with pytest.raises(OfflineValidationError):
        validate_release(**second)


def test_single_reviewer_cannot_count_twice_when_policy_requires_distinct_reviewers(bundle):
    args, keys = bundle
    # The same human can possess two authorized scopes, but cannot satisfy separation of duties.
    reviewer = (
        args["registry"]
        .reviewers[0]
        .model_copy(
            update={
                "scopes": args["registry"].reviewers[0].scopes
                + tuple(
                    scope.model_copy(update={"role": "legal"})
                    for scope in args["registry"].reviewers[0].scopes
                )
            }
        )
    )
    registry = ReviewerRegistry(reviewers=(reviewer,), keys=(args["registry"].keys[0],))
    second = signed(
        keys,
        "clinical",
        scope=ApprovalScope(purpose="care", language="en", role="legal", action="approve"),
    )
    evidence = (args["evidence"][0], second)
    refs = tuple(item.digest() for item in evidence)
    item = args["artifacts"][0].model_copy(update={"approval_references": refs})
    entry = (
        args["manifest"]
        .entries[0]
        .model_copy(update={"evidence_digests": refs, "artifact_digest": artifact_digest(item)})
    )
    transition = args["manifest"].transitions[0].model_copy(update={"evidence_digests": refs})
    manifest = args["manifest"].model_copy(
        update={"entries": (entry,), "transitions": (transition,)}
    )
    updated = args | dict(
        registry=registry, artifacts=(item,), evidence=evidence, manifest=manifest
    )
    with pytest.raises(OfflineValidationError, match="duplicate_approval_authority"):
        validate_release(**updated)


def test_canonical_timestamp_offsets_and_transport_whitespace_are_unambiguous(bundle):
    args, _ = bundle
    record = args["evidence"][0]
    same_instant = datetime(2026, 1, 2, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    normalized = record.model_copy(update={"decided_at": same_instant})
    assert normalized.signing_bytes() == record.signing_bytes()
    assert verify(normalized, args)
    altered = record.model_copy(update={"expires_at": record.expires_at + timedelta(seconds=1)})
    with pytest.raises(OfflineValidationError, match="invalid_signature"):
        verify(altered, args)


@pytest.mark.parametrize(
    "changes", [{"language": "fr"}, {"notice_version": ""}, {"purpose": "unknown"}]
)
def test_evidence_identity_uses_existing_notice_contract(bundle, changes):
    args, _ = bundle
    data = args["evidence"][0].model_dump(mode="json")
    data["identity"].update(changes)
    with pytest.raises(ValueError, match="Invalid offline document"):
        parse_document(ApprovalEvidence, json.dumps(data).encode())


def test_missing_or_empty_policy_roles_fail_closed(bundle):
    args, _ = bundle
    with pytest.raises(ValidationError):
        ReleaseTrustPolicy(
            requirements=(), distinct_reviewers=True, allow_genesis=True, allow_reactivation=False
        )
    with pytest.raises(ValidationError):
        RoleRequirement(purpose="care", action="approve", roles=())
    with pytest.raises(OfflineValidationError, match="invalid_time"):
        validate_release(**(args | {"at": NOW.replace(tzinfo=None)}))


def test_sanitized_failures_do_not_include_submitted_values(bundle):
    args, _ = bundle
    record = args["evidence"][0].model_copy(update={"reviewer_id": "PRIVATE_SUBMITTED_VALUE"})
    with pytest.raises(OfflineValidationError) as error:
        verify(record, args)
    assert "PRIVATE_SUBMITTED_VALUE" not in str(error.value)
    with pytest.raises(ValueError) as error:
        parse_document(ApprovalEvidence, b'{"private":"PRIVATE_SUBMITTED_VALUE"}')
    assert "PRIVATE_SUBMITTED_VALUE" not in str(error.value)
