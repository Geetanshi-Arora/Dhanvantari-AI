"""Separate application RLS actors from Supabase's shared database role."""

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute((Path(__file__).parent / "0003_runtime_isolation.sql").read_text())


def downgrade():
    raise RuntimeError("Security downgrade disabled; use a reviewed forward migration")
