"""Offline release contracts. Trust roots and authoritative history are external inputs.

No file discovery, production approval policy, signing key or activation is supplied.
Canonical JSON is sorted, compact, ASCII-escaped, with aware timestamps normalized
 to UTC microseconds. Signatures cover a domain-separated canonical payload, never
raw transport JSON. No Unicode or notice-text normalization is performed.
"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.consent_notices import NoticeArtifact, NoticeContract, NoticeIdentity
from app.schemas.patients import ConsentPurpose, Language

Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Action = Literal["approve", "retire", "reactivate"]


def canonical_bytes(value) -> bytes:
    def convert(item):
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("Aware timestamp required")
            return item.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if isinstance(item, dict):
            return {key: convert(val) for key, val in item.items()}
        if isinstance(item, (tuple, list)):
            return [convert(val) for val in item]
        if item is None or type(item) in (str, bool, int):
            return item
        raise ValueError("Unsupported canonical value")

    return json.dumps(
        convert(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


class ReleaseContract(NoticeContract):
    @field_validator("*", mode="before")
    @classmethod
    def explicit_timestamp(cls, value):
        if type(value) in (float, int):
            raise ValueError("Numeric values are not permitted")
        return value


class ApprovalScope(ReleaseContract):
    purpose: ConsentPurpose
    language: Language
    role: Identifier
    action: Action


class ApprovalEvidence(ReleaseContract):
    identity: NoticeIdentity
    content_hash: Digest
    reviewer_id: Identifier
    scope: ApprovalScope
    decision: Literal["approved", "rejected"]
    decided_at: AwareDatetime
    expires_at: AwareDatetime
    release_id: Identifier
    previous_manifest_digest: Digest | None
    algorithm: Literal["Ed25519"]
    key_id: Identifier
    signature: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{128}$")]

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.scope.purpose != self.identity.purpose
            or self.scope.language != self.identity.language
        ):
            raise ValueError("Evidence scope does not match identity")
        if self.expires_at <= self.decided_at:
            raise ValueError("Invalid evidence validity interval")
        return self

    def signing_bytes(self) -> bytes:
        return b"dhanvantari.notice-evidence.v1\x00" + canonical_bytes(
            self.model_dump(mode="python", exclude={"signature"})
        )

    def digest(self) -> str:
        return sha256(
            b"dhanvantari.evidence-record.v1\x00" + canonical_bytes(self.model_dump(mode="python"))
        ).hexdigest()


class TrustedReviewer(ReleaseContract):
    reviewer_id: Identifier
    scopes: Annotated[tuple[ApprovalScope, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_scopes(self):
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("Duplicate trusted scope")
        return self


class TrustedKey(ReleaseContract):
    key_id: Identifier
    reviewer_id: Identifier
    algorithm: Literal["Ed25519"]
    public_key: Digest  # 32 raw public-key bytes, hex encoded; never a private key.
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    revoked: StrictBool

    @model_validator(mode="after")
    def interval(self):
        if self.valid_until <= self.valid_from:
            raise ValueError("Invalid trusted key interval")
        return self


class ReviewerRegistry(ReleaseContract):
    reviewers: Annotated[tuple[TrustedReviewer, ...], Field(max_length=1000)] = ()
    keys: Annotated[tuple[TrustedKey, ...], Field(max_length=2000)] = ()

    @model_validator(mode="after")
    def unambiguous(self):
        ids = [r.reviewer_id for r in self.reviewers]
        if len(set(ids)) != len(ids) or len({k.key_id for k in self.keys}) != len(self.keys):
            raise ValueError("Duplicate trust identity")
        if len({k.public_key for k in self.keys}) != len(self.keys):
            raise ValueError("Reused trusted key material")
        if any(k.reviewer_id not in ids for k in self.keys):
            raise ValueError("Trusted key has no reviewer")
        return self


class RoleRequirement(ReleaseContract):
    purpose: ConsentPurpose
    action: Action
    roles: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def unique_roles(self):
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("Duplicate required role")
        return self


class ReleaseTrustPolicy(ReleaseContract):
    requirements: Annotated[tuple[RoleRequirement, ...], Field(min_length=1, max_length=20)]
    distinct_reviewers: StrictBool
    allow_genesis: StrictBool
    allow_reactivation: StrictBool

    @model_validator(mode="after")
    def unique_requirements(self):
        ids = [(r.purpose, r.action) for r in self.requirements]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate trust requirement")
        return self


class ReleaseEntry(ReleaseContract):
    identity: NoticeIdentity
    content_hash: Digest
    artifact_digest: Digest
    evidence_digests: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=100)]
    state: Literal["active", "retired"]
    state_changed_at: AwareDatetime

    @field_validator("evidence_digests")
    @classmethod
    def unique_evidence(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate evidence reference")
        return values


class LifecycleTransition(ReleaseContract):
    identity: NoticeIdentity
    action: Literal["activate", "retire", "reactivate"]
    evidence_digests: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=20)]

    @field_validator("evidence_digests")
    @classmethod
    def unique_evidence(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate transition evidence")
        return values


class ReleaseManifest(ReleaseContract):
    release_id: Identifier
    created_at: AwareDatetime
    previous_manifest_digest: Digest | None
    entries: Annotated[tuple[ReleaseEntry, ...], Field(min_length=1, max_length=1000)]
    transitions: Annotated[tuple[LifecycleTransition, ...], Field(max_length=1000)]

    @model_validator(mode="after")
    def unique_entries(self):
        for values in (self.entries, self.transitions):
            if len({item.identity for item in values}) != len(values):
                raise ValueError("Duplicate manifest identity")
        return self

    def digest(self) -> str:
        return sha256(
            b"dhanvantari.notice-manifest.v1\x00" + canonical_bytes(self.model_dump(mode="python"))
        ).hexdigest()


class TrustedReleaseHistory(ReleaseContract):
    """Caller MUST source this checkpoint from an authenticated authoritative ledger.

    Internal consistency does not prove completeness, freshness or authenticity.
    Full-snapshot manifests retain every historical identity and evidence digest.
    """

    head: ReleaseManifest | None
    head_digest: Digest | None
    release_ids: tuple[Identifier, ...]

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.release_ids)) != len(self.release_ids):
            raise ValueError("Duplicate historical release")
        if self.head is None:
            if self.head_digest is not None or self.release_ids:
                raise ValueError("Invalid genesis checkpoint")
        elif self.head.digest() != self.head_digest or self.head.release_id not in self.release_ids:
            raise ValueError("Invalid historical checkpoint")
        return self


def artifact_digest(artifact: NoticeArtifact) -> str:
    """Bind immutable content and original approval metadata; lifecycle is separate."""
    artifact = NoticeArtifact.model_validate(artifact)
    return sha256(
        b"dhanvantari.notice-artifact.v1\x00"
        + canonical_bytes(artifact.model_dump(mode="python", exclude={"retired", "retired_at"}))
    ).hexdigest()


def parse_document(model, raw: bytes):
    """Bounded UTF-8 JSON only, rejecting duplicate keys and non-standard constants.

    No paths, files, directories or external references are resolved.
    """

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Invalid JSON constant")

    if not isinstance(raw, bytes) or len(raw) > 2_000_000:
        raise ValueError("Invalid offline document")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError
        return model.model_validate(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("Invalid offline document") from None


PRODUCTION_REVIEWER_REGISTRY = ReviewerRegistry()
