"""Offline guard tests: no cluster constructors with approval, SQL or sockets."""

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "backend/tests/disposable"
SPEC = importlib.util.spec_from_file_location(
    "offline_pg_guard", HARNESS / "dhanvantari_pg_guard.py"
)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
SOCKET = Path("/tmp/dhanvantari-pg-fictional/socket")
CREDENTIALS = {"postgres": "fictional-test-password"}


def local_url():
    return URL.create(
        "postgresql+psycopg",
        username="postgres",
        password=CREDENTIALS["postgres"],
        database="postgres",
        query={"host": str(SOCKET), "port": "5432"},
    )


def local_parameters():
    return dict(
        host=str(SOCKET),
        port="5432",
        dbname="postgres",
        user="postgres",
        password=CREDENTIALS["postgres"],
        connect_timeout=5,
    )


def test_no_start_without_explicit_approval(monkeypatch):
    operation = Mock(side_effect=AssertionError("No external operation allowed"))
    monkeypatch.setattr(guard.subprocess, "run", operation)
    monkeypatch.setattr(guard.tempfile, "mkdtemp", operation)
    with pytest.raises(guard.HarnessError, match="Explicit"):
        guard.DisposablePostgres()
    operation.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGPASSWORD",
        "PGPASSFILE",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGOPTIONS",
        "SUPABASE_DB_URL",
        "OTHER_DATABASE_URL",
        "OTHER_DB_DSN",
    ],
)
def test_unexpected_environment_fails_without_echoing_values(name):
    with pytest.raises(guard.HarnessError) as error:
        guard.reject_external_environment({name: "FICTIONAL_PRIVATE_MARKER"})
    assert "FICTIONAL_PRIVATE_MARKER" not in str(error.value)


def test_only_exact_internal_migration_override_is_permitted():
    guard.reject_external_environment(
        {"MIGRATION_DATABASE_URL": "fictional-internal"}, "fictional-internal"
    )
    with pytest.raises(guard.HarnessError):
        guard.reject_external_environment(
            {"MIGRATION_DATABASE_URL": "fictional-other"}, "fictional-internal"
        )


def test_owned_socket_url_and_roundtrip_are_accepted_without_connecting():
    url = local_url()
    assert guard.validate_target(url, SOCKET, CREDENTIALS) == url
    assert (
        guard.validate_target(url.render_as_string(hide_password=False), SOCKET, CREDENTIALS) == url
    )
    guard.validate_connect_parameters(local_parameters(), SOCKET, CREDENTIALS)


def test_sqlalchemy_url_is_compatible_with_psycopg_conninfo_parser():
    parameters = guard.conninfo_to_dict(
        local_url()
        .render_as_string(hide_password=False)
        .replace("postgresql+psycopg://", "postgresql://", 1)
    )
    guard.validate_connect_parameters(parameters, SOCKET, CREDENTIALS)


def test_driver_wrappers_and_dotenv_guard_with_mocked_connections(monkeypatch):
    # This deliberately bypasses construction/startup and invokes only a Mock
    # instead of libpq. It verifies Psycopg's patchability without any DB access.
    monkeypatch.setattr(guard.os, "environ", {})
    original = Mock(return_value="fictional-mocked-connection")
    monkeypatch.setattr(guard.psycopg, "connect", original)
    server = object.__new__(guard.DisposablePostgres)
    server.socket = SOCKET
    server._credentials = CREDENTIALS.copy()
    server._engines = []
    server._stack = None
    try:
        server._install_guards()
        parameters = local_parameters()
        del parameters["connect_timeout"]
        assert guard.psycopg.connect(**parameters) == "fictional-mocked-connection"
        assert original.call_args.kwargs["connect_timeout"] == "5"
        assert guard.psycopg.Connection.connect(**parameters) == "fictional-mocked-connection"
        before = original.call_count
        with pytest.raises(guard.HarnessError):
            guard.psycopg.connect(**{**parameters, "host": "fictional.supabase.co"})
        assert original.call_count == before
        import dotenv
        import sqlalchemy

        assert dotenv.load_dotenv("must-not-be-read.env") is False
        with pytest.raises(guard.HarnessError):
            sqlalchemy.create_engine(
                "postgresql+psycopg://fictional@fictional.supabase.co/postgres"
            )
    finally:
        server._stack.close()


@pytest.fixture
def mocked_driver(monkeypatch):
    """Only a Mock can be reached; never create a cluster or call libpq."""
    monkeypatch.setattr(guard.os, "environ", {})
    original = Mock(return_value="fictional-mocked-connection")
    monkeypatch.setattr(guard.psycopg, "connect", original)
    server = object.__new__(guard.DisposablePostgres)
    server.socket = SOCKET
    server._credentials = CREDENTIALS.copy()
    server._engines = []
    server._stack = None
    try:
        server._install_guards()
        yield original
    finally:
        server._stack.close()


@pytest.mark.parametrize("use_classmethod", [False, True])
def test_real_sqlalchemy_adapter_map_is_forwarded_separately(
    mocked_driver, monkeypatch, use_classmethod
):
    engine = guard.create_engine(local_url(), hide_parameters=True)
    try:
        args, parameters = engine.dialect.create_connect_args(local_url())
        assert not args
        context = parameters["context"]
        assert type(context) is guard.AdaptersMap
        validate = Mock(wraps=guard.validate_connect_parameters)
        monkeypatch.setattr(guard, "validate_connect_parameters", validate)
        connect = guard.psycopg.Connection.connect if use_classmethod else guard.psycopg.connect
        assert connect(**parameters) == "fictional-mocked-connection"
        assert mocked_driver.call_args.kwargs["context"] is context
        checked_parameters = validate.call_args.args[0]
        assert "context" not in checked_parameters
        assert set(checked_parameters) == {
            "host",
            "port",
            "dbname",
            "user",
            "password",
            "connect_timeout",
        }
        assert mocked_driver.call_args.kwargs["host"] == str(SOCKET)
        assert mocked_driver.call_args.kwargs["connect_timeout"] == "5"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "context", [None, {}, object(), SimpleNamespace(adapters={}), "FICTIONAL_PRIVATE_MARKER"]
)
def test_non_map_python_context_is_rejected_without_echoing_input(mocked_driver, context):
    with pytest.raises(guard.HarnessError, match="Invalid disposable adapter context") as error:
        guard.psycopg.connect(**local_parameters(), context=context)
    assert "FICTIONAL_PRIVATE_MARKER" not in str(error.value)
    mocked_driver.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "fictional.supabase.co"},
        {"host": str(SOCKET) + ",fictional.supabase.co"},
        {"hostaddr": "127.0.0.1"},
        {"service": "fictional"},
        {"port": "5433"},
        {"dbname": "other"},
        {"user": "other"},
        {"password": "wrong"},
        {"options": "-c search_path=public"},
        {"row_factory": object()},
        {"unknown_option": "fictional"},
    ],
)
def test_adapter_context_cannot_bypass_target_or_unknown_parameter_checks(mocked_driver, changes):
    parameters = {**local_parameters(), **changes, "context": guard.AdaptersMap()}
    with pytest.raises(guard.HarnessError, match="Non-disposable database connection rejected"):
        guard.psycopg.connect(**parameters)
    mocked_driver.assert_not_called()


def test_context_cannot_be_smuggled_through_libpq_conninfo(mocked_driver):
    with pytest.raises(guard.HarnessError, match="Invalid disposable connection parameters"):
        guard.psycopg.connect("context=fictional", **local_parameters())
    mocked_driver.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "localhost"},
        {"host": "127.0.0.1"},
        {"host": "::1"},
        {"host": "fictional.supabase.co"},
        {"database": "other"},
        {"username": "service_role"},
        {"password": "wrong"},
        {"drivername": "postgresql"},
        {"port": 5432},
        {"query": {"host": "/tmp/other-server", "port": "5432"}},
        {"query": {"host": str(SOCKET), "port": "5432", "service": "fictional"}},
        {"query": {"host": str(SOCKET), "port": "5432", "hostaddr": "127.0.0.1"}},
    ],
)
def test_non_owned_or_remote_url_rejected(changes):
    with pytest.raises(guard.HarnessError):
        guard.validate_target(local_url().set(**changes), SOCKET, CREDENTIALS)


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "fictional.supabase.co"},
        {"host": str(SOCKET) + ",fictional.supabase.co"},
        {"host": "localhost"},
        {"hostaddr": "127.0.0.1"},
        {"service": "fictional"},
        {"options": "-c search_path=public"},
        {"dbname": "other"},
        {"user": "other"},
        {"password": "wrong"},
        {"password": None},
        {"port": "5433"},
        {"connect_timeout": 0},
    ],
)
def test_driver_guard_rejects_overrides_and_fallbacks(changes):
    with pytest.raises(guard.HarnessError):
        guard.validate_connect_parameters({**local_parameters(), **changes}, SOCKET, CREDENTIALS)


def test_python_network_and_dns_fail_before_any_socket_connection():
    with guard.no_network():
        with pytest.raises(guard.HarnessError, match="Network access disabled"):
            guard.socket.create_connection(("fictional.supabase.co", 5432))
        with pytest.raises(guard.HarnessError, match="Network access disabled"):
            guard.socket.getaddrinfo("fictional.supabase.co", 5432)


@pytest.mark.parametrize("status,deleted", [(3, True), (0, False), (1, False)])
def test_cleanup_only_deletes_after_confirmed_stop(monkeypatch, status, deleted):
    server = object.__new__(guard.DisposablePostgres)
    server._closed = False
    server._engines = [SimpleNamespace(dispose=Mock(side_effect=RuntimeError("fictional")))]
    server._start_attempted = True
    server.directory = Path("/tmp/dhanvantari-pg-fictional")
    server.data = server.directory / "data"
    server._credentials = {"postgres": "fictional-test-password"}
    server._stack = None
    server._run = Mock(
        side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=status)]
    )
    remove = Mock()
    monkeypatch.setattr(guard.shutil, "rmtree", remove)
    if deleted:
        server.close()
        assert server._closed and not server._credentials
        remove.assert_called_once_with(server.directory)
    else:
        with pytest.raises(guard.HarnessError, match="retained"):
            server.close()
        remove.assert_not_called()
    assert server._run.call_count == 2


def test_fixture_gates_execution_and_always_registers_cleanup():
    source = (HARNESS / "conftest.py").read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    approval = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "getoption"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "--run-disposable-postgres"
    )
    start = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "start"
    )
    assert approval < start
    assert any(isinstance(node, ast.Try) and node.finalbody for node in ast.walk(tree))
    assert "server.close()" in source
    source = (HARNESS / "dhanvantari_pg_guard.py").read_text()
    tree = ast.parse(source)
    patches = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "patch"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert "dotenv.load_dotenv" in patches
    assert "sqlalchemy.create_engine" in patches
    assert "--auth-host=reject" in source
    assert "-h ''" in source
    assert "atexit.register(self.close)" in source
