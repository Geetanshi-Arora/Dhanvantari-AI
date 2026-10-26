"""Pure offline validation against caller-supplied trust roots and ledger checkpoint.

Success is NOT publication, legal approval, DB acceptance, replay authorization or
permission to enable patient registration. No I/O or application integration exists.
"""

from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.schemas.consent_notices import NoticeArtifact, NoticeIdentity
from app.schemas.consent_releases import (
    ApprovalEvidence,
    ReleaseManifest,
    ReleaseTrustPolicy,
    ReviewerRegistry,
    TrustedReleaseHistory,
    artifact_digest,
)


class OfflineValidationError(ValueError):
    """Fixed categories only, without supplied content or signature material."""


def fail(category):
    raise OfflineValidationError(category)


def validated(model, value):
    try:
        if not isinstance(value, model):
            raise ValueError
        return model.model_validate(value)
    except (ValueError, TypeError):
        fail("invalid_contract")


def aware_now(at):
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        fail("invalid_time")


def verify_evidence(
    evidence,
    *,
    identity,
    content_hash,
    action,
    release_id,
    previous_manifest_digest,
    registry,
    at,
) -> ApprovalEvidence:
    """Verify one current decision, not completeness of required human approvals."""
    evidence = validated(ApprovalEvidence, evidence)
    identity = validated(NoticeIdentity, identity)
    registry = validated(ReviewerRegistry, registry)
    aware_now(at)
    if (
        evidence.identity != identity
        or evidence.content_hash != content_hash
        or evidence.scope.action != action
        or evidence.release_id != release_id
        or evidence.previous_manifest_digest != previous_manifest_digest
    ):
        fail("evidence_binding")
    if evidence.decision != "approved" or not evidence.decided_at <= at < evidence.expires_at:
        fail("evidence_not_currently_approved")
    reviewer = next((r for r in registry.reviewers if r.reviewer_id == evidence.reviewer_id), None)
    key = next((k for k in registry.keys if k.key_id == evidence.key_id), None)
    if (
        reviewer is None
        or key is None
        or key.reviewer_id != evidence.reviewer_id
        or evidence.scope not in reviewer.scopes
    ):
        fail("untrusted_reviewer_key_or_scope")
    if (
        key.revoked
        or not key.valid_from <= evidence.decided_at < key.valid_until
        or not key.valid_from <= at < key.valid_until
    ):
        fail("untrusted_key_interval")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key)).verify(
            bytes.fromhex(evidence.signature), evidence.signing_bytes()
        )
    except (InvalidSignature, ValueError, TypeError):
        fail("invalid_signature")
    return evidence


@dataclass(frozen=True, slots=True)
class OfflineReleaseCheck:
    manifest_digest: str
    checked_entries: int

    # Constants are properties, not caller-overridable authorization flags.
    @property
    def publication_authorized(self):
        return False

    @property
    def new_decisions_authorized(self):
        return False


def validate_release(manifest, *, artifacts, evidence, registry, policy, history, at):
    """Validate a full snapshot relative to EXTERNALLY AUTHENTICATED history.

    This cannot detect a forged/stale ledger supplied by its caller. Genesis must
    be explicitly authorized by supplied policy; missing history never means genesis.
    Historical evidence is retained by digest, not reclassified by today's expiry
    or key rotation. Every new transition requires currently trusted signed evidence.
    """
    manifest = validated(ReleaseManifest, manifest)
    registry = validated(ReviewerRegistry, registry)
    policy = validated(ReleaseTrustPolicy, policy)
    history = validated(TrustedReleaseHistory, history)
    aware_now(at)
    if not registry.keys or not registry.reviewers:
        fail("missing_trust_roots")
    if manifest.created_at > at or manifest.release_id in history.release_ids:
        fail("invalid_release_identity_or_time")
    if manifest.previous_manifest_digest != history.head_digest:
        fail("release_continuity")
    if history.head is None:
        if not policy.allow_genesis:
            fail("genesis_not_authorized")
    elif manifest.created_at <= history.head.created_at:
        fail("release_time_order")
    if type(artifacts) is not tuple or type(evidence) is not tuple:
        fail("explicit_snapshot_required")
    entries = {item.identity: item for item in manifest.entries}
    prior = {item.identity: item for item in history.head.entries} if history.head else {}
    transitions = {item.identity: item for item in manifest.transitions}
    artifact_map = {}
    for raw in artifacts:
        item = validated(NoticeArtifact, raw)
        if item.identity in artifact_map:
            fail("duplicate_artifact")
        artifact_map[item.identity] = item
    if set(artifact_map) != set(entries) or not set(prior) <= set(entries):
        fail("historical_artifact_missing")
    if not set(transitions) <= set(entries):
        fail("unknown_transition")
    records = {}
    decisions = set()
    for raw in evidence:
        item = validated(ApprovalEvidence, raw)
        key = (item.identity, item.scope.action, item.release_id, item.reviewer_id, item.scope.role)
        digest = item.digest()
        if digest in records or key in decisions:
            fail("duplicate_or_conflicting_evidence")
        records[digest] = item
        decisions.add(key)
    referenced = {digest for entry in entries.values() for digest in entry.evidence_digests}
    if set(records) != referenced:
        fail("evidence_inventory_mismatch")
    requirements = {(r.purpose, r.action): set(r.roles) for r in policy.requirements}
    for identity, entry in entries.items():
        item = artifact_map[identity]
        old = prior.get(identity)
        transition = transitions.get(identity)
        if not item.approved or item.approved_at > manifest.created_at:
            fail("artifact_not_approved")
        if (
            entry.content_hash != item.content_hash
            or entry.artifact_digest != artifact_digest(item)
            or item.retired != (entry.state == "retired")
            or not set(item.approval_references) <= set(entry.evidence_digests)
        ):
            fail("artifact_manifest_mismatch")
        if any(
            records[d].identity != identity or records[d].content_hash != entry.content_hash
            for d in entry.evidence_digests
        ):
            fail("historical_evidence_binding")
        if (identity.purpose, "approve") not in requirements:
            fail("missing_required_roles")
        if old:
            if (
                entry.content_hash != old.content_hash
                or entry.artifact_digest != old.artifact_digest
                or not set(old.evidence_digests) <= set(entry.evidence_digests)
            ):
                fail("historical_identity_changed")
            if entry.state == old.state:
                if transition is not None or entry != old:
                    fail("unrequested_lifecycle_change")
                if item.retired and item.retired_at != entry.state_changed_at:
                    fail("retirement_timestamp_mismatch")
                continue
            expected = "retire" if entry.state == "retired" else "reactivate"
            if expected == "reactivate" and not policy.allow_reactivation:
                fail("reactivation_not_authorized")
        else:
            expected = "activate"
            if entry.state != "active":
                fail("initial_state_not_active")
        if transition is None or transition.action != expected:
            fail("missing_or_incorrect_transition")
        if entry.state_changed_at != manifest.created_at:
            fail("lifecycle_timestamp_mismatch")
        if item.retired and item.retired_at != entry.state_changed_at:
            fail("retirement_timestamp_mismatch")
        selected = set(transition.evidence_digests)
        previous_refs = set(old.evidence_digests) if old else set()
        if set(entry.evidence_digests) != previous_refs | selected or selected & previous_refs:
            fail("lifecycle_evidence_mismatch")
        action = "approve" if expected == "activate" else expected
        roles = requirements.get((identity.purpose, action))
        if not roles:
            fail("missing_required_roles")
        seen_roles = set()
        seen_reviewers = set()
        for digest in transition.evidence_digests:
            if digest not in records:
                fail("missing_evidence")
            record = verify_evidence(
                records[digest],
                identity=identity,
                content_hash=entry.content_hash,
                action=action,
                release_id=manifest.release_id,
                previous_manifest_digest=history.head_digest,
                registry=registry,
                at=at,
            )
            if record.decided_at > manifest.created_at or record.scope.role not in roles:
                fail("approval_policy_mismatch")
            if record.scope.role in seen_roles or (
                policy.distinct_reviewers and record.reviewer_id in seen_reviewers
            ):
                fail("duplicate_approval_authority")
            seen_roles.add(record.scope.role)
            seen_reviewers.add(record.reviewer_id)
        if seen_roles != roles:
            fail("missing_required_roles")
        if old is None and (
            set(item.approval_references) != selected
            or item.approved_at != max(records[d].decided_at for d in selected)
        ):
            fail("original_approval_metadata_mismatch")
    return OfflineReleaseCheck(manifest.digest(), len(entries))
