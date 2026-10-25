from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


def make_engine(url: str):
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


@contextmanager
def actor_session(engine, actor_id: UUID):
    """SET LOCAL prevents identity leaking to another request through pooled connections."""
    if not isinstance(actor_id, UUID):
        raise ValueError("A verified UUID is required")
    with Session(engine) as db, db.begin():
        db.execute(text("SET LOCAL ROLE dhanvantari_clinical_actor"))
        db.execute(
            text("SELECT set_config('request.jwt.claim.sub', :uid, true)"), {"uid": str(actor_id)}
        )
        yield db


def check_runtime_role(engine):
    with engine.connect() as connection:
        safe = connection.execute(
            text("""
            SELECT rolcanlogin AND rolinherit
              AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole
              AND NOT rolcreatedb AND NOT rolreplication
              AND NOT EXISTS (SELECT 1 FROM pg_roles privileged
                WHERE (privileged.rolsuper OR privileged.rolbypassrls
                       OR privileged.rolcreaterole OR privileged.rolcreatedb
                       OR privileged.rolreplication)
                  AND pg_has_role(current_user, privileged.oid, 'MEMBER'))
              AND NOT EXISTS (SELECT 1 FROM pg_roles extra
                WHERE extra.rolname NOT IN (
                  current_user, 'dhanvantari_api', 'dhanvantari_clinical_actor')
                  AND pg_has_role(current_user, extra.oid, 'MEMBER'))
              AND pg_has_role(current_user, 'dhanvantari_api', 'MEMBER')
              AND pg_has_role(current_user, 'dhanvantari_api', 'USAGE')
              AND pg_has_role(current_user, 'dhanvantari_clinical_actor', 'USAGE')
              AND NOT EXISTS (SELECT 1 FROM pg_roles groups
                WHERE groups.rolname IN ('dhanvantari_api', 'dhanvantari_clinical_actor')
                  AND groups.rolcanlogin)
              AND NOT EXISTS (SELECT 1 FROM pg_auth_members membership
                WHERE membership.admin_option
                  AND pg_has_role(current_user, membership.member, 'MEMBER'))
              AND NOT pg_has_role(current_user, 'app_owner', 'MEMBER')
              AND NOT has_schema_privilege(current_user, 'app', 'CREATE')
              AND NOT has_schema_privilege(current_user, 'public', 'CREATE')
              AND NOT EXISTS (SELECT 1 FROM pg_class object
                JOIN pg_namespace namespace ON namespace.oid=object.relnamespace
                WHERE namespace.nspname='app'
                  AND pg_has_role(current_user, object.relowner, 'MEMBER'))
            FROM pg_roles WHERE rolname = current_user
        """)
        ).scalar()
        if not safe:
            raise RuntimeError("DATABASE_URL must use the restricted runtime role")
        # Exercise the actual switch, including PostgreSQL 16+ SET membership options.
        connection.execute(text("SET LOCAL ROLE dhanvantari_clinical_actor"))
        if connection.execute(text("SELECT current_user")).scalar() != "dhanvantari_clinical_actor":
            raise RuntimeError("Runtime cannot assume the clinical actor role")
