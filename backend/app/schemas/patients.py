"""Patient contracts only; no persistence or clinical authorization is performed here.

Request validation requires context={'approved_consent_notices': frozenset(...)}.
Entries are exact (purpose, notice_version, language) tuples supplied by trusted
server configuration. No notice is approved by default. Historical responses do
not consult that catalogue: a withdrawn notice must remain representable.
Optional unanswered purposes are omitted from the consents list, never defaulted.
"""

from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

Language = Literal["en", "hi", "bn", "ta", "te", "mr", "gu", "pa"]
ConsentPurpose = Literal["care", "uploads", "future_ai"]
Name = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80)
]
NoticeVersion = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=40)]
Age = Annotated[int, Field(strict=True, ge=18, le=120)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_nul(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("NUL characters are not permitted")
        return value


class ConsentDecision(Contract):
    purpose: ConsentPurpose
    granted: StrictBool
    notice_version: NoticeVersion
    language: Language

    @model_validator(mode="after")
    def approved_notice(self, info: ValidationInfo) -> Self:
        catalogue = (info.context or {}).get("approved_consent_notices")
        if (
            not isinstance(catalogue, frozenset)
            or (self.purpose, self.notice_version, self.language) not in catalogue
        ):
            raise ValueError("Consent notice or translation is not approved")
        return self


class PatientRegistrationRequest(Contract):
    name: Name
    age: Age
    preferred_language: Language
    sex: Literal["Female", "Male", "Intersex", "Prefer not to say"] | None = None
    contact_reference: Annotated[str, StringConstraints(strict=True, max_length=60)] | None = None
    history: Annotated[str, StringConstraints(strict=True, max_length=1000)] | None = None
    allergies: Annotated[str, StringConstraints(strict=True, max_length=500)] | None = None
    current_medicines: Annotated[str, StringConstraints(strict=True, max_length=500)] | None = None
    consents: Annotated[list[ConsentDecision], Field(min_length=1, max_length=3)]

    @field_validator(
        "contact_reference", "history", "allergies", "current_medicines", mode="before"
    )
    @classmethod
    def optional_text(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def explicit_care(self) -> Self:
        decisions = {decision.purpose: decision for decision in self.consents}
        if len(decisions) != len(self.consents) or "care" not in decisions:
            raise ValueError("Exactly one care decision and no duplicate purposes are required")
        if not decisions["care"].granted and any(
            value is not None for value in (self.history, self.allergies, self.current_medicines)
        ):
            raise ValueError("Clinical information requires care consent")
        return self


class ConsentUpdateRequest(ConsentDecision):
    # Required even when null; enables optimistic concurrency for first decisions.
    expected_current_id: UUID | None


class ConsentRevocationRequest(Contract):
    # The current consent UUID is supplied in the endpoint path.
    purpose: ConsentPurpose


class ConsentResponse(Contract):
    id: UUID
    purpose: ConsentPurpose
    status: Literal["granted", "declined", "revoked"]
    granted: StrictBool
    notice_version: NoticeVersion
    language: Language
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        expected = (
            "revoked"
            if self.revoked_at is not None
            else ("granted" if self.granted else "declined")
        )
        if self.status != expected:
            raise ValueError("Inconsistent consent status")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("Revocation predates the decision")
        return self


class PatientDirectoryItem(Contract):
    id: UUID
    reference: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=30)]
    name: Name
    age: Age
    age_recorded_on: date
    preferred_language: Language


class PatientDirectoryResponse(Contract):
    items: Annotated[list[PatientDirectoryItem], Field(max_length=100)]
    next_cursor: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)] | None


class PatientDetailResponse(PatientDirectoryItem):
    """Explicit projection of a care-consented fictional adult patient only."""

    sex: Literal["Female", "Male", "Intersex", "Prefer not to say"] | None
    contact_reference: Annotated[str, StringConstraints(strict=True, max_length=60)] | None
    history: Annotated[str, StringConstraints(strict=True, max_length=1000)] | None
    allergies: Annotated[str, StringConstraints(strict=True, max_length=500)] | None
    current_medicines: Annotated[str, StringConstraints(strict=True, max_length=500)] | None
    created_at: AwareDatetime


class RegistrationStatusResponse(Contract):
    id: UUID
    reference: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=30)]
    preferred_language: Language
    created_at: AwareDatetime
    care_access: Literal["enabled", "blocked"]
    consents: Annotated[list[ConsentResponse], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def consistent_access(self) -> Self:
        current = {consent.purpose: consent for consent in self.consents}
        if len(current) != len(self.consents) or "care" not in current:
            raise ValueError("A care decision and unique current purposes are required")
        expected = "enabled" if current["care"].status == "granted" else "blocked"
        if self.care_access != expected:
            raise ValueError("Inconsistent care access")
        return self


class PatientRegistrationResponse(RegistrationStatusResponse):
    pass


class ConsentMutationResponse(Contract):
    consent: ConsentResponse
    care_access: Literal["enabled", "blocked"]

    @model_validator(mode="after")
    def consistent_care(self) -> Self:
        if self.consent.purpose == "care":
            expected = "enabled" if self.consent.status == "granted" else "blocked"
            if self.care_access != expected:
                raise ValueError("Inconsistent care access")
        return self
