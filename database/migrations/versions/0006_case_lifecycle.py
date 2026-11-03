"""Create a case for each registered patient and retain status history."""

from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parent / "0006_case_lifecycle.sql").read_text())


def downgrade():
    raise RuntimeError("Security downgrade disabled; use a reviewed forward migration")
