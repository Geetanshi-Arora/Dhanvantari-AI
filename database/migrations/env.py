import os
from pathlib import Path

from alembic import context
from app.db.migration_security import protect_migration_ledger
from app.models import clinical  # noqa: F401
from app.models.foundation import Base
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def run_migrations():
    load_dotenv(Path(__file__).resolve().parents[2] / "backend" / ".env")
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url or not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Set MIGRATION_DATABASE_URL to a PostgreSQL migration-owner connection")

    if context.is_offline_mode():
        raise RuntimeError("Offline migrations cannot verify ledger security")
    else:
        engine = create_engine(url, hide_parameters=True)
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                include_schemas=True,
                version_table_schema="public",
                transaction_per_migration=False,
            )
            with context.begin_transaction():
                connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                context.run_migrations()
                # Alembic current uses dont_mutate; inspection must not repair ACLs.
                if not context.get_context().opts.get("dont_mutate", False):
                    protect_migration_ledger(connection)
        engine.dispose()


try:
    run_migrations()
except Exception:
    raise RuntimeError(
        "Migration failed; check private configuration and database permissions"
    ) from None
