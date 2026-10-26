"""Narrow patient registration and consent interfaces; no approved notices seeded."""

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parent / "0004_patient_registration.sql").read_text())


def downgrade():
    raise RuntimeError("Security downgrade disabled; use a reviewed forward migration")
