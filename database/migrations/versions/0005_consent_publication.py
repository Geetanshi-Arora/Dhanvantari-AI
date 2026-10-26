"""Private, inactive publication bookkeeping only; no publication entry points."""

from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parent / "0005_consent_publication.sql").read_text())


def downgrade():
    raise RuntimeError("Security downgrade disabled; use a reviewed forward migration")
