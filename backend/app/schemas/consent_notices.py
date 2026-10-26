"""Offline notice contracts, not approval authority or a publication mechanism.

Nothing here loads files, populates the patient-service catalogue or authorizes a
clinical operation. Evidence references are opaque identifiers, not proof of human
approval. SHA-256 binds exact UTF-8 text, without whitespace/Unicode normalization.
"""

from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.schemas.patients import ConsentPurpose, Language, NoticeVersion

Reference = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]


class NoticeContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )

    @field_validator("*", mode="before")
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str) and ("\x00" in value or not value.strip()):
            raise ValueError("Invalid notice text field")
        return value


class NoticeIdentity(NoticeContract):
    purpose: ConsentPurpose
    notice_version: NoticeVersion
    language: Language


class NoticeArtifact(NoticeIdentity):
    # Defensive resource bounds; these are not approved legal-content limits.
    text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=65536)]
    content_hash: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    approval_references: Annotated[tuple[Reference, ...], Field(min_length=1, max_length=20)]
    approved: StrictBool
    approved_at: AwareDatetime | None
    retired: StrictBool
    retired_at: AwareDatetime | None

    @field_validator("text")
    @classmethod
    def utf8_text(cls, value):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Invalid notice text encoding") from None
        return value

    @field_validator("approval_references")
    @classmethod
    def evidence(cls, values):
        if any(not value.strip() or "\x00" in value for value in values):
            raise ValueError("Invalid approval evidence reference")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate approval evidence reference")
        return values

    @model_validator(mode="after")
    def integrity(self) -> Self:
        if sha256(self.text.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("Notice content hash mismatch")
        if self.approved != (self.approved_at is not None):
            raise ValueError("Inconsistent approval metadata")
        if self.retired != (self.retired_at is not None):
            raise ValueError("Inconsistent retirement metadata")
        if self.retired and (not self.approved or self.retired_at < self.approved_at):
            raise ValueError("Invalid retirement chronology")
        return self

    @property
    def identity(self) -> NoticeIdentity:
        return NoticeIdentity(
            purpose=self.purpose, notice_version=self.notice_version, language=self.language
        )


class NoticeCatalogue(NoticeContract):
    artifacts: Annotated[tuple[NoticeArtifact, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def unique_identity(self) -> Self:
        identities = [(item.purpose, item.notice_version, item.language) for item in self.artifacts]
        # Reject identical duplicates too: no order-dependent metadata selection.
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate notice identity")
        return self

    def historical(self, identity: NoticeIdentity) -> NoticeArtifact | None:
        """Exact lookup retains retired artifacts; does not approve any new decision."""
        identity = NoticeIdentity.model_validate(identity)
        catalogue = NoticeCatalogue.model_validate(self)
        return next((item for item in catalogue.artifacts if item.identity == identity), None)


class NoticeReadiness(NoticeContract):
    identity: NoticeIdentity | None = None
    reason: Literal[
        "missing", "unapproved", "retired", "not_yet_approved", "invalid", "available"
    ] = "missing"

    @computed_field
    @property
    def available(self) -> bool:
        return self.reason == "available" and self.identity is not None

    def validation_payload(self) -> dict:
        """JSON-compatible round-trip convention; derived availability is recomputed.

        Validate this payload with model_validate(), or JSON-encode it for
        model_validate_json(). Ordinary response dumps include `available` and are
        intentionally not accepted as input: unknown fields remain forbidden.
        """
        return self.model_dump(mode="json", exclude={"available"})


# Immutable and empty. Never import fictional fixtures or load operator content.
PRODUCTION_NOTICE_CATALOGUE = NoticeCatalogue()


def notice_readiness(
    purpose,
    notice_version,
    language,
    *,
    at: datetime,
    catalogue: NoticeCatalogue = PRODUCTION_NOTICE_CATALOGUE,
) -> NoticeReadiness:
    """Pure artifact availability only; database accepts_new remains authoritative.

    Invalid inputs fail closed. Exact language is mandatory; no fallback, selection
    of another version, caching, registration enabling or database I/O occurs here.
    """
    try:
        identity = NoticeIdentity(purpose=purpose, notice_version=notice_version, language=language)
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError
        if not isinstance(catalogue, NoticeCatalogue):
            raise ValueError
        artifact = catalogue.historical(identity)
    except (ValueError, TypeError):
        return NoticeReadiness(reason="invalid")
    reason = "available"
    if artifact is None:
        reason = "missing"
    elif not artifact.approved:
        reason = "unapproved"
    elif artifact.retired:
        reason = "retired"
    elif artifact.approved_at > at:
        reason = "not_yet_approved"
    return NoticeReadiness(identity=identity, reason=reason)
