"""Consent isolation and decision integrity; no clinical write privileges added."""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parent / "0002_security.sql").read_text())


def downgrade():
    raise RuntimeError("Security downgrade disabled; use a reviewed forward migration")
