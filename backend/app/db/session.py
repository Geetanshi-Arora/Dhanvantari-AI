from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


def make_engine(url: str):
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


@contextmanager
def actor_session(engine, actor_id: UUID):
    """SET LOCAL prevents identity leaking to another request through pooled connections."""
    with Session(engine) as db, db.begin():
        db.execute(text("SET LOCAL ROLE authenticated"))
        db.execute(
            text("SELECT set_config('request.jwt.claim.sub', :uid, true)"), {"uid": str(actor_id)}
        )
        yield db


def check_runtime_role(engine):
    with engine.connect() as connection:
        safe = connection.execute(
            text("""
            SELECT NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole
              AND NOT rolcreatedb AND NOT rolreplication
              AND NOT EXISTS (SELECT 1 FROM pg_roles privileged
                WHERE (privileged.rolsuper OR privileged.rolbypassrls
                       OR privileged.rolcreaterole OR privileged.rolcreatedb
                       OR privileged.rolreplication)
                  AND pg_has_role(current_user, privileged.oid, 'MEMBER'))
              AND NOT EXISTS (SELECT 1 FROM pg_roles extra
                WHERE extra.rolname NOT IN (current_user, 'dhanvantari_api', 'authenticated')
                  AND pg_has_role(current_user, extra.oid, 'MEMBER'))
              AND pg_has_role(current_user, 'dhanvantari_api', 'MEMBER')
              AND NOT pg_has_role(current_user, 'app_owner', 'MEMBER')
            FROM pg_roles WHERE rolname = current_user
        """)
        ).scalar()
        if not safe:
            raise RuntimeError("DATABASE_URL must use the restricted runtime role")
