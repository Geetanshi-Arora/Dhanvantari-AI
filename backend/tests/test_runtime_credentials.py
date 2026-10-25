import traceback
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from conftest import sign_in
from sqlalchemy import event

from app.core import config
from app.core import runtime_credentials as credentials
from app.core.config import Settings, load_runtime_settings
from app.main import create_app

PASSWORD = "Fictional_" + "a" * 54


def dpapi_settings(settings, tmp_path, **overrides):
    certificate = tmp_path / "ca.crt"
    certificate.write_text("fictional certificate; no connection attempted")
    values = settings.model_dump()
    values.update(
        database_url=None,
        supabase_url="https://dgrmddokpylbjgpcqrem.supabase.co",
        runtime_credential_file=str(tmp_path / "runtime.dpapi"),
        runtime_pooler_host="aws-0-fictional.pooler.supabase.com",
        runtime_ca_file=str(certificate),
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_in_memory_url(settings, tmp_path, monkeypatch):
    private = dpapi_settings(settings, tmp_path)
    monkeypatch.setattr(credentials, "_read_password", lambda path: PASSWORD)
    url = credentials.runtime_database_url(private)
    assert url.password == PASSWORD
    assert PASSWORD not in str(url)
    assert url.username == "dhanvantari_runtime.dgrmddokpylbjgpcqrem"
    assert url.port == 5432 and url.database == "postgres"
    assert dict(url.query) == {"sslmode": "verify-full", "sslrootcert": private.runtime_ca_file}


@pytest.mark.parametrize(
    "overrides",
    [
        {"database_url": "postgresql+psycopg://fictional/unused"},
        {"runtime_pooler_host": "localhost"},
        {"runtime_pooler_host": "x.pooler.supabase.com.evil"},
        {"supabase_url": "https://other.supabase.co"},
        {"runtime_ca_file": None},
        {"environment": "production"},
    ],
)
def test_runtime_configuration_conflicts(settings, tmp_path, overrides):
    with pytest.raises(ValueError):
        dpapi_settings(settings, tmp_path, **overrides)


def test_loader_and_factory_hide_private_errors(settings, tmp_path, monkeypatch):
    private = dpapi_settings(settings, tmp_path)
    marker = "FICTIONAL-PRIVATE-ERROR"

    def fail(path):
        raise OSError(marker)

    monkeypatch.setattr(credentials, "_read_password", fail)
    for call in [lambda: credentials.runtime_database_url(private), lambda: create_app(private)]:
        with pytest.raises(RuntimeError) as exc:
            call()
        assert marker not in "".join(traceback.format_exception(exc.value))


def test_missing_certificate_does_not_decrypt(settings, tmp_path, monkeypatch):
    private = dpapi_settings(settings, tmp_path)
    from pathlib import Path

    Path(private.runtime_ca_file).unlink()
    reader = Mock()
    monkeypatch.setattr(credentials, "_read_password", reader)
    with pytest.raises(RuntimeError):
        credentials.runtime_database_url(private)
    reader.assert_not_called()


def test_dedicated_configuration_does_not_open_admin_env(settings, tmp_path, monkeypatch):
    for key in ["DATABASE_URL", "MIGRATION_DATABASE_URL"]:
        monkeypatch.delenv(key, raising=False)
    private = dpapi_settings(settings, tmp_path)
    runtime = tmp_path / "runtime.env"
    # The administrative file is deliberately invalid and must never be parsed.
    (tmp_path / ".env").write_text("DATABASE_URL=FICTIONAL_ADMIN_SECRET\n")
    runtime.write_text(
        "\n".join(
            f"{key.upper()}="
            f"{value.get_secret_value() if hasattr(value, 'get_secret_value') else value}"
            for key, value in private.model_dump().items()
            if value is not None
        )
    )
    monkeypatch.setattr(config, "RUNTIME_FILE", runtime)
    monkeypatch.chdir(tmp_path)
    loaded = load_runtime_settings()
    assert loaded.database_url is None
    assert loaded.runtime_pooler_host == private.runtime_pooler_host
    runtime.write_text(runtime.read_text() + "\nMIGRATION_DATABASE_URL=FICTIONAL_ADMIN_SECRET\n")
    with pytest.raises(ValueError):
        load_runtime_settings()


@pytest.mark.parametrize(
    "key",
    [
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "PGHOSTADDR",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGOPTIONS",
        "PGPASSWORD",
        "PGPASSFILE",
    ],
)
def test_server_rejects_database_url_environment_override(monkeypatch, key):
    monkeypatch.setenv(key, "FICTIONAL_PRIVATE_OVERRIDE")
    reader = Mock()
    monkeypatch.setattr(config, "dotenv_values", reader)
    with pytest.raises(ValueError):
        load_runtime_settings()
    reader.assert_not_called()


def test_configuration_interpolation_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    path = tmp_path / "runtime.env"
    path.touch()
    monkeypatch.setattr(config, "RUNTIME_FILE", path)
    reader = Mock(return_value={})
    monkeypatch.setattr(config, "dotenv_values", reader)
    with pytest.raises(ValueError):
        load_runtime_settings()
    reader.assert_called_once_with(path, interpolate=False)


def test_dpapi_rejects_unavailable_windows_api_without_creating_a_credential(tmp_path):
    path = tmp_path / "nonexistent.dpapi"
    with pytest.raises(RuntimeError, match="private Windows settings"):
        credentials._read_password(path)
    assert not path.exists()


def test_auth_verification_precedes_actor_identity(client, monkeypatch):
    import app.core.security as security
    import app.routes.auth as routes

    verified = []
    real_user = client.auth_provider.user
    real_actor = routes.actor_session

    def user(token):
        identity = real_user(token)
        verified.append(identity)
        return identity

    @contextmanager
    def actor(engine, identity):
        assert verified and identity == verified.pop(0)
        with real_actor(engine, identity) as db:
            yield db

    monkeypatch.setattr(client.auth_provider, "user", user)
    monkeypatch.setattr(routes, "actor_session", actor)
    monkeypatch.setattr(security, "actor_session", actor)
    assert sign_in(client).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    client.auth_provider.failure = 503
    assert client.get("/api/v1/auth/me").status_code == 503
    assert verified == []


def test_readiness_asserts_no_identity(client, dbs):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(dbs[1], "before_cursor_execute", record)
    try:
        assert client.get("/api/v1/health/ready").status_code == 200
    finally:
        event.remove(dbs[1], "before_cursor_execute", record)
    assert statements == ["SELECT 1"]


@pytest.mark.parametrize("location", ["relative", "checkout"])
def test_unsafe_credential_location_never_reaches_loader(settings, tmp_path, monkeypatch, location):
    from pathlib import Path

    path = (
        "relative.dpapi"
        if location == "relative"
        else str(Path(credentials.__file__).resolve().parents[3] / "unused.dpapi")
    )
    private = dpapi_settings(settings, tmp_path, runtime_credential_file=path)
    reader = Mock()
    monkeypatch.setattr(credentials, "_read_password", reader)
    with pytest.raises(RuntimeError):
        credentials.runtime_database_url(private)
    reader.assert_not_called()


def test_symlink_configuration_is_not_opened(tmp_path, monkeypatch):
    for key in ["DATABASE_URL", "MIGRATION_DATABASE_URL"]:
        monkeypatch.delenv(key, raising=False)
    administrative = tmp_path / ".env"
    administrative.write_text("MIGRATION_DATABASE_URL=FICTIONAL_ADMIN_SECRET")
    runtime = tmp_path / "runtime.env"
    runtime.symlink_to(administrative)
    monkeypatch.setattr(config, "RUNTIME_FILE", runtime)
    reader = Mock()
    monkeypatch.setattr(config, "dotenv_values", reader)
    with pytest.raises(ValueError):
        load_runtime_settings()
    reader.assert_not_called()


def test_mismatched_verified_identity_never_reaches_clinical_session(client, monkeypatch):
    from conftest import OTHER

    import app.core.security as security

    assert sign_in(client).status_code == 200
    client.auth_provider.identity = OTHER
    actor = Mock(side_effect=AssertionError("No clinical identity should be applied"))
    monkeypatch.setattr(security, "actor_session", actor)
    assert client.get("/api/v1/auth/me").status_code == 401
    actor.assert_not_called()


def test_unverified_login_never_reaches_clinical_session(client, monkeypatch):
    import app.routes.auth as routes

    client.auth_provider.failure = 401
    actor = Mock(side_effect=AssertionError("No clinical identity should be applied"))
    monkeypatch.setattr(routes, "actor_session", actor)
    assert sign_in(client).status_code == 401
    actor.assert_not_called()
