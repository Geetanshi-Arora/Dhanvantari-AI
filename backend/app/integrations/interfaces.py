"""Future integration contracts only. No provider implementation or network calls."""

from typing import Protocol
from uuid import UUID


class AssessmentProvider(Protocol):
    def assess(self, *, visit_id: UUID, original_text: str, language: str) -> dict:
        """Return unapproved suggestions; never create clinician decisions or prescriptions."""
        ...


class VoiceProvider(Protocol):
    def transcribe(self, *, audio: bytes, language: str) -> str: ...
    def synthesize(self, *, text: str, language: str) -> bytes: ...


class NotificationProvider(Protocol):
    def notify_review(self, *, clinician_id: UUID, secure_case_url: str) -> None:
        """No patient text or identifiers in external notification content."""
        ...
