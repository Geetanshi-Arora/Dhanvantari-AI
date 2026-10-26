"""Offline catalogue snapshot; no startup, file loading, DB or publication hooks.

Historical presence is NOT authorization, approval evidence verification or proof
of an idempotent retry. PostgreSQL alone decides replay eligibility and accepts_new.
No runtime PatientService catalogue is derived or modified by this adapter.
"""

from dataclasses import dataclass
from datetime import datetime

from app.schemas.consent_notices import (
    PRODUCTION_NOTICE_CATALOGUE,
    NoticeArtifact,
    NoticeCatalogue,
    NoticeIdentity,
    NoticeReadiness,
    notice_readiness,
)


@dataclass(frozen=True, slots=True)
class ConsentCatalogueAdapter:
    catalogue: NoticeCatalogue = PRODUCTION_NOTICE_CATALOGUE

    def __post_init__(self):
        if not isinstance(self.catalogue, NoticeCatalogue):
            raise ValueError("Invalid notice catalogue")
        # Revalidate nested instances, including unvalidated model_copy/construct.
        # Capture a separate, frozen schema snapshot instead of mutable input data.
        object.__setattr__(self, "catalogue", NoticeCatalogue.model_validate(self.catalogue))

    def historical_lookup(self, purpose, notice_version, language) -> NoticeArtifact | None:
        """Exact lookup, including retired/unapproved artifacts; not authorization.

        Invalid identities raise validation errors. A valid absent identity returns
        None. No fallback to a different language, purpose or version is permitted.
        """
        identity = NoticeIdentity(purpose=purpose, notice_version=notice_version, language=language)
        return self.catalogue.historical(identity)

    def historical_member(self, purpose, notice_version, language) -> bool:
        """Presence only; must never be used as approval for new decisions or retries."""
        return self.historical_lookup(purpose, notice_version, language) is not None

    def new_decision_readiness(
        self, purpose, notice_version, language, *, at: datetime
    ) -> NoticeReadiness:
        """Artifact readiness, not DB acceptance or a registration authorization."""
        return notice_readiness(purpose, notice_version, language, at=at, catalogue=self.catalogue)


# Empty immutable production snapshot. No fixture or runtime service imports.
PRODUCTION_CONSENT_ADAPTER = ConsentCatalogueAdapter()
