import os
from pathlib import Path
from uuid import UUID

import pgserver
import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.main import create_app
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

NURSE = UUID("10000000-0000-0000-0000-000000000001")
DOCTOR = UUID("10000000-0000-0000-0000-000000000002")
OTHER = UUID("10000000-0000-0000-0000-000000000003")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def postgres(tmp_path_factory):
    server = pgserver.get_server(
        tmp_path_factory.mktemp("postgres") / "data", cleanup_mode="delete"
    )
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://")
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = url
    cfg = Config(str(ROOT / "database" / "alembic.ini"))
    command.upgrade(cfg, "head")
    admin = create_engine(url, hide_parameters=True)
    with admin.begin() as c:
        c.execute(text("CREATE ROLE test_runtime LOGIN NOSUPERUSER NOBYPASSRLS"))
        c.execute(text("GRANT dhanvantari_api TO test_runtime"))
    # Unix-socket connection using the generated test login (no shared credentials).
    from sqlalchemy.engine import make_url

    runtime_url = make_url(url).set(username="test_runtime")
    runtime = create_engine(runtime_url, hide_parameters=True)
    yield admin, runtime, cfg
    runtime.dispose()
    admin.dispose()
    if previous is None:
        os.environ.pop("MIGRATION_DATABASE_URL", None)
    else:
        os.environ["MIGRATION_DATABASE_URL"] = previous
    server.cleanup()


@pytest.fixture
def dbs(postgres):
    admin, runtime, _ = postgres
    with admin.begin() as c:
        tables = c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='app'")).scalars()
        c.execute(text("TRUNCATE " + ",".join("app." + t for t in tables) + " CASCADE"))
        for uid, role, name in [
            (NURSE, "nurse", "Nurse Example"),
            (DOCTOR, "doctor", "Doctor Example"),
            (OTHER, "nurse", "Other Example"),
        ]:
            c.execute(
                text("INSERT INTO app.staff_profiles(id,role,display_name) VALUES (:id,:r,:n)"),
                {"id": uid, "r": role, "n": name},
            )
            table = "doctors" if role == "doctor" else "nurses"
            c.execute(
                text(f"INSERT INTO app.{table}(id,registration_number) VALUES (:id,:reg)"),
                {"id": uid, "reg": "FICTIONAL-" + str(uid)},
            )
    return admin, runtime


class FakeAuth:
    identity = NURSE
    failure = None

    def login(self, email, password):
        if password != "fictional-test-password":
            raise HTTPException(401, "Invalid or expired credentials")
        return "fictional-provider-token", 3600

    def user(self, token):
        if self.failure:
            raise HTTPException(self.failure, "Authentication unavailable")
        return self.identity


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused/unused",
        supabase_url="https://fictional.supabase.co",
        supabase_publishable_key="fictional-publishable-key",
        session_encryption_key=Fernet.generate_key().decode(),
        allowed_origin="http://localhost:8000",
        cookie_secure=False,
    )


@pytest.fixture
def client(dbs, settings):
    auth = FakeAuth()
    app = create_app(settings, engine=dbs[1], auth=auth)
    with TestClient(app, base_url="http://localhost:8000") as client:
        client.auth_provider = auth
        yield client


def sign_in(client, **overrides):
    return client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:8000"},
        json={"email": "nurse@example.com", "password": "fictional-test-password", **overrides},
    )
