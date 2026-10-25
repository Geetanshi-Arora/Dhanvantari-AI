from pathlib import Path
from unittest.mock import patch

import pgserver
import pytest
from alembic import command
from alembic.config import Config
from conftest import DOCTOR, NURSE, OTHER
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from test_database import seed_case

from app.db.migration_security import protect_migration_ledger
from app.db.session import actor_session, check_runtime_role

ROOT = Path(__file__).resolve().parents[2]
CLINICAL = "dhanvantari_clinical_actor"


@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    """Independent local cluster: never use the operator's configured URL."""
    server = pgserver.get_server(tmp_path / "data", cleanup_mode="delete")
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    engine = create_engine(url, hide_parameters=True)
    cfg = Config(str(ROOT / "database/alembic.ini"))
    with engine.begin() as c:
        for role in ("anon", "authenticated", "service_role"):
            c.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        # Simulate Supabase-style creator defaults. Do not change real defaults.
        c.execute(
            text("""ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT ALL ON TABLES TO anon,authenticated,service_role""")
        )
    try:
        yield engine, cfg
    finally:
        engine.dispose()
        server.cleanup()


def acl(c):
    return c.execute(
        text("""SELECT x.grantee,x.privilege_type,x.is_grantable
        FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x
        WHERE c.oid='public.alembic_version'::regclass
        ORDER BY x.grantee,x.privilege_type""")
    ).all()


@pytest.mark.parametrize("existing_ledger", [False, True])
def test_current_is_read_only_even_when_ledger_needs_protection(migration_db, existing_ledger):
    engine, cfg = migration_db
    if existing_ledger:
        command.upgrade(cfg, "0002")
        with engine.begin() as c:
            c.execute(text("GRANT UPDATE ON public.alembic_version TO authenticated"))
            before = acl(c)

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        with patch("app.db.migration_security.protect_migration_ledger") as safeguard:
            command.current(cfg)
            safeguard.assert_not_called()
    finally:
        event.remove(Engine, "before_cursor_execute", capture)

    assert statements
    assert set(statements) <= {"SELECT", "SHOW", "SET"}
    with engine.connect() as c:
        if existing_ledger:
            assert acl(c) == before
            assert (
                c.execute(text("SELECT version_num FROM public.alembic_version")).scalar() == "0002"
            )
        else:
            assert c.execute(text("SELECT to_regclass('public.alembic_version')")).scalar() is None


@pytest.mark.parametrize("operation", ["upgrade", "noop_upgrade", "stamp"])
@pytest.mark.parametrize("fail_verification", [False, True])
def test_write_commands_protect_ledger_before_commit(migration_db, operation, fail_verification):
    engine, cfg = migration_db
    command.upgrade(cfg, "0002")
    with engine.begin() as c:
        c.execute(text("GRANT UPDATE ON public.alembic_version TO authenticated"))
        before_acl = acl(c)

    expected_revision = {"upgrade": "0003", "noop_upgrade": "0002", "stamp": "0001"}[operation]

    def safeguard(connection):
        assert connection.in_transaction()
        assert (
            connection.execute(text("SELECT version_num FROM public.alembic_version")).scalar()
            == expected_revision
        )
        protect_migration_ledger(connection)
        # A separate connection still sees the old ACL before this transaction commits.
        with engine.connect() as observer:
            assert acl(observer) == before_acl
        if fail_verification:
            raise RuntimeError("Fictional verification failure")

    def execute():
        if operation == "stamp":
            # Exercise a real bookkeeping change only in this disposable database.
            command.stamp(cfg, "0001")
        else:
            command.upgrade(cfg, "head" if operation == "upgrade" else "0002")

    with patch(
        "app.db.migration_security.protect_migration_ledger", side_effect=safeguard
    ) as guard:
        if fail_verification:
            with pytest.raises(RuntimeError, match="Migration failed"):
                execute()
        else:
            execute()
        guard.assert_called_once()

    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM public.alembic_version")).scalar() == (
            "0002" if fail_verification else expected_revision
        )
        if fail_verification:
            assert acl(c) == before_acl
        else:
            assert not c.execute(
                text(
                    "SELECT has_table_privilege('authenticated','public.alembic_version','UPDATE')"
                )
            ).scalar()


def test_upgrade_preserves_policies_and_managed_permissions(migration_db):
    engine, cfg = migration_db
    command.upgrade(cfg, "0002")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE public.fictional_managed_object(id int)"))
        before = c.execute(
            text("""SELECT polname,pg_get_expr(polqual,polrelid)
            FROM pg_policy WHERE polrelid IN
            (SELECT oid FROM pg_class WHERE relnamespace='app'::regnamespace)
            ORDER BY polname""")
        ).all()
        defaults = c.execute(
            text("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid")
        ).all()
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM public.alembic_version")).scalar() == "0003"
        assert (
            c.execute(
                text("SELECT pg_has_role('dhanvantari_api','authenticated','MEMBER')")
            ).scalar()
            is False
        )
        assert c.execute(
            text("SELECT pg_has_role('dhanvantari_api',:r,'USAGE')"), {"r": CLINICAL}
        ).scalar()
        assert (
            c.execute(
                text("""SELECT polname,pg_get_expr(polqual,polrelid)
            FROM pg_policy WHERE polrelid IN
            (SELECT oid FROM pg_class WHERE relnamespace='app'::regnamespace)
            ORDER BY polname""")
            ).all()
            == before
        )
        assert (
            c.execute(text("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid")).all()
            == defaults
        )
        for role in ("anon", "authenticated", "service_role"):
            assert c.execute(
                text("SELECT has_table_privilege(:r,'public.fictional_managed_object','UPDATE')"),
                {"r": role},
            ).scalar()
            assert not c.execute(
                text("SELECT has_table_privilege(:r,'public.alembic_version','SELECT')"),
                {"r": role},
            ).scalar()
        assert (
            c.execute(
                text("SELECT count(*) FROM pg_policy WHERE CAST(:r AS regrole)=ANY(polroles)"),
                {"r": CLINICAL},
            ).scalar()
            == 14
        )
        assert not c.execute(
            text("SELECT has_schema_privilege('authenticated','app','USAGE')")
        ).scalar()
    command.upgrade(cfg, "head")


def test_fresh_upgrade_and_recreated_ledger_are_protected(migration_db):
    engine, cfg = migration_db
    with engine.begin() as c:
        c.execute(
            text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO PUBLIC")
        )
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        original_acl = acl(c)
        assert original_acl
        # Exercise creation under unchanged permissive defaults on an existing schema.
        c.execute(text("DROP TABLE public.alembic_version"))
    command.stamp(cfg, "head")
    with engine.connect() as c:
        assert acl(c) == original_acl
        assert (
            c.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")).scalar() == 20
        )


def test_ledger_verification_failure_rolls_back_revision_and_roles(migration_db):
    engine, cfg = migration_db
    command.upgrade(cfg, "0002")
    with engine.begin() as c:
        c.execute(text("GRANT UPDATE ON public.alembic_version TO authenticated"))
        before = acl(c)
    real_guard = protect_migration_ledger

    def fail_after_revocation(connection):
        real_guard(connection)
        raise RuntimeError("Fictional verification failure")

    with patch("app.db.migration_security.protect_migration_ledger", fail_after_revocation):
        with pytest.raises(RuntimeError, match="Migration failed"):
            command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM public.alembic_version")).scalar() == "0002"
        assert acl(c) == before
        assert not c.execute(
            text("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname=:r)"), {"r": CLINICAL}
        ).scalar()
        assert c.execute(
            text("SELECT pg_has_role('dhanvantari_api','authenticated','MEMBER')")
        ).scalar()


def test_guard_refuses_unknown_ledger_grants(migration_db):
    engine, cfg = migration_db
    command.upgrade(cfg, "0002")
    with engine.begin() as c:
        c.execute(text("CREATE ROLE fictional_unreviewed NOLOGIN"))
        c.execute(text("GRANT SELECT ON public.alembic_version TO fictional_unreviewed"))
    with pytest.raises(RuntimeError, match="Migration failed"):
        command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM public.alembic_version")).scalar() == "0002"


def test_actor_switch_and_identity_reset_on_single_pooled_connection(dbs):
    seed_case(dbs[0])
    engine = create_engine(dbs[1].url, pool_size=1, max_overflow=0)
    try:
        check_runtime_role(engine)
        pids = []
        for identity in (NURSE, OTHER, DOCTOR):
            with actor_session(engine, identity) as db:
                pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar())
                assert db.execute(text("SELECT current_user")).scalar() == CLINICAL
                assert db.execute(text("SELECT app.actor_id()")).scalar() == identity
                assert db.execute(text("SELECT count(*) FROM app.patients")).scalar() == (
                    0 if identity == OTHER else 1
                )
        with pytest.raises(DBAPIError), actor_session(engine, NURSE) as db:
            db.execute(text("SELECT 1/0"))
        with pytest.raises(RuntimeError), actor_session(engine, DOCTOR):
            raise RuntimeError("Fictional service failure")
        with engine.begin() as c:
            assert c.execute(text("SELECT current_user")).scalar() == "dhanvantari_runtime"
            assert c.execute(text("SELECT app.actor_id()")).scalar() is None
            assert c.execute(text("SELECT count(*) FROM app.patients")).scalar() == 0
            assert c.execute(text("SELECT pg_backend_pid()")).scalar() == pids[0]
        assert len(set(pids)) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "SET LOCAL ROLE authenticated",
        "SET LOCAL ROLE app_owner",
        "UPDATE app.urgent_escalations SET status='acknowledged'",
        "SELECT * FROM public.alembic_version",
    ],
)
def test_runtime_cannot_access_shared_roles_or_clinical_writes(dbs, statement):
    with pytest.raises(DBAPIError), dbs[1].begin() as c:
        c.execute(text(statement))


@pytest.mark.parametrize(
    "change,restore",
    [
        (
            "GRANT authenticated TO dhanvantari_runtime",
            "REVOKE authenticated FROM dhanvantari_runtime",
        ),
        (
            "GRANT dhanvantari_api TO dhanvantari_runtime WITH ADMIN OPTION",
            "REVOKE ADMIN OPTION FOR dhanvantari_api FROM dhanvantari_runtime",
        ),
        ("ALTER ROLE dhanvantari_runtime NOINHERIT", "ALTER ROLE dhanvantari_runtime INHERIT"),
    ],
)
def test_runtime_guard_rejects_unsafe_membership_options(dbs, change, restore):
    with dbs[0].begin() as c:
        c.execute(text(change))
    try:
        with pytest.raises(RuntimeError):
            check_runtime_role(dbs[1])
    finally:
        with dbs[0].begin() as c:
            c.execute(text(restore))
    check_runtime_role(dbs[1])


def test_actor_requires_uuid_before_database_checkout(dbs):
    with pytest.raises(ValueError), actor_session(dbs[1], "unverified"):
        pass


def test_non_superuser_migration_administrator(migration_db, monkeypatch):
    engine, cfg = migration_db
    with engine.begin() as c:
        c.execute(
            text("""CREATE ROLE fictional_migrator LOGIN INHERIT
            CREATEROLE NOSUPERUSER NOBYPASSRLS""")
        )
        c.execute(text("GRANT CREATE ON DATABASE postgres TO fictional_migrator"))
        c.execute(text("GRANT USAGE, CREATE ON SCHEMA public TO fictional_migrator"))
        c.execute(text("GRANT authenticated TO fictional_migrator WITH ADMIN OPTION"))
        c.execute(
            text("""ALTER DEFAULT PRIVILEGES FOR ROLE fictional_migrator
            IN SCHEMA public GRANT ALL ON TABLES TO anon,authenticated,service_role""")
        )
    url = engine.url.set(username="fictional_migrator")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM public.alembic_version")).scalar() == "0003"
        assert (
            c.execute(
                text("""SELECT pg_get_userbyid(relowner) FROM pg_class
            WHERE oid='public.alembic_version'::regclass""")
            ).scalar()
            == "fictional_migrator"
        )
        assert not c.execute(
            text("SELECT has_table_privilege('authenticated','public.alembic_version','UPDATE')")
        ).scalar()


def test_runtime_guard_requires_set_role_option(dbs):
    with dbs[0].connect() as c:
        if c.dialect.server_version_info < (16,):
            pytest.skip("SET membership options require PostgreSQL 16+")
    with dbs[0].begin() as c:
        c.execute(text("GRANT dhanvantari_api TO dhanvantari_runtime WITH SET FALSE"))
    try:
        with pytest.raises(DBAPIError):
            check_runtime_role(dbs[1])
    finally:
        with dbs[0].begin() as c:
            c.execute(text("GRANT dhanvantari_api TO dhanvantari_runtime WITH SET TRUE"))
    check_runtime_role(dbs[1])


@pytest.mark.parametrize("schema", ["app", "public"])
def test_runtime_guard_rejects_schema_creation_privileges(dbs, schema):
    with dbs[0].begin() as c:
        c.execute(text(f"GRANT CREATE ON SCHEMA {schema} TO dhanvantari_api"))
    try:
        with pytest.raises(RuntimeError):
            check_runtime_role(dbs[1])
    finally:
        with dbs[0].begin() as c:
            c.execute(text(f"REVOKE CREATE ON SCHEMA {schema} FROM dhanvantari_api"))
    check_runtime_role(dbs[1])


def test_ledger_guard_rejects_inherited_maintain_privilege(migration_db):
    engine, cfg = migration_db
    with engine.connect() as c:
        if c.dialect.server_version_info < (17,):
            pytest.skip("MAINTAIN requires PostgreSQL 17+")
    command.upgrade(cfg, "0002")
    with engine.begin() as c:
        c.execute(text("GRANT pg_maintain TO authenticated"))
    with (
        pytest.raises(RuntimeError, match="Application ledger access remains"),
        engine.begin() as c,
    ):
        protect_migration_ledger(c)
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM public.alembic_version")).scalar() == "0002"
