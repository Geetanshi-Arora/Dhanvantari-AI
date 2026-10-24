"""Foundation schema, restricted roles and fail-closed clinical tables."""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    root = Path(__file__).parent
    op.execute((root / "0001_roles.sql").read_text())
    op.execute((root / "0001_tables.sql").read_text())
    op.execute((root / "0001_security.sql").read_text())


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a reviewed backup instead")
