import os
from pathlib import Path

from alembic import context
from app.models import clinical  # noqa: F401
from app.models.foundation import Base
from dotenv import load_dotenv
from sqlalchemy import create_engine


def run_migrations():
    load_dotenv(Path(__file__).resolve().parents[2] / "backend" / ".env")
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url or not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Set MIGRATION_DATABASE_URL to a PostgreSQL migration-owner connection")

    if context.is_offline_mode():
        context.configure(
            url=url, target_metadata=Base.metadata, literal_binds=True, include_schemas=True
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(url, hide_parameters=True)
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=Base.metadata, include_schemas=True
            )
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()


try:
    run_migrations()
except Exception:
    raise RuntimeError(
        "Migration failed; check private configuration and database permissions"
    ) from None
