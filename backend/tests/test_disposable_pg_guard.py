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


@pytest.mark.parametrize("revision", ["0003", "0004", "0005"])
def test_explicit_migration_allowlist_with_mocked_alembic_only(monkeypatch, revision):
    monkeypatch.setattr(guard.os, "environ", {})
    server = object.__new__(guard.DisposablePostgres)
    server.socket = SOCKET
    server._credentials = CREDENTIALS.copy()
    config = object()
    monkeypatch.setattr(guard, "Config", Mock(return_value=config))
    upgrade = Mock()
    monkeypatch.setattr(guard.command, "upgrade", upgrade)
    server.migrate(revision)
    upgrade.assert_called_once_with(config, revision)
    assert "MIGRATION_DATABASE_URL" not in guard.os.environ


@pytest.mark.parametrize(
    "revision", ["0001", "0002", "0006", "head", "base", "+1", "0005 ", "../0005", "remote"]
)
def test_unapproved_migration_target_rejected_before_any_operation(monkeypatch, revision):
    server = object.__new__(guard.DisposablePostgres)
    upgrade = Mock(side_effect=AssertionError("Unapproved operation"))
    monkeypatch.setattr(guard.command, "upgrade", upgrade)
    with pytest.raises(guard.HarnessError, match="Unreviewed migration target"):
        server.migrate(revision)
    upgrade.assert_not_called()


def test_existing_guard_logic_changes_only_allowlist_and_startup_diagnostic_hook():
    relative = "backend/tests/disposable/dhanvantari_pg_guard.py"
    committed = guard.subprocess.check_output(
        [
            "git",
            "--no-optional-locks",
            "show",
            "445d9650365f2b2744e47c054e6b12a555690cc3:" + relative,
        ],
        cwd=ROOT,
    ).replace(b"\r\n", b"\n")
    old = b'if revision not in {"0003", "0004"}:'
    new = b'if revision not in {"0003", "0004", "0005"}:'
    assert committed.count(old) == 1
    expected = (
        committed.replace(old, new)
        .decode()
        .replace(
            'raise HarnessError("Disposable cluster startup failed")',
            "raise HarnessError(_startup_diagnostic(result, self.directory, self.owner)) from None",
        )
    )
    expected_tree = ast.parse(expected)
    actual_tree = ast.parse((ROOT / relative).read_text())
    # Every original function and complete class body remains identical apart
    # from those two approved changes, including cleanup/connection enforcement.
    for original in expected_tree.body:
        if isinstance(original, (ast.FunctionDef, ast.ClassDef)):
            actual = next(
                node
                for node in actual_tree.body
                if isinstance(node, type(original)) and node.name == original.name
            )
            assert ast.dump(actual) == ast.dump(original)
    added = {node.name for node in actual_tree.body if isinstance(node, ast.FunctionDef)} - {
        node.name for node in expected_tree.body if isinstance(node, ast.FunctionDef)
    }
    assert added == {"_safe_startup_log", "_startup_diagnostic"}


DIAGNOSTIC_DIR = Path("/tmp/dhanvantari-pg-fictional")
CATEGORIES = [
    (b"FATAL: permission denied", "permission_denied"),
    (b"FATAL: could not bind Unix-domain socket", "socket_error"),
    (b"FATAL: could not create shared memory segment", "shared_memory_error"),
    (b"FATAL: unrecognized configuration parameter", "configuration_error"),
    (b"pg_ctl: server did not start in time", "startup_timeout"),
]


def diagnostic_fields(value):
    fields = dict(item.split("=", 1) for item in value.split())
    assert set(fields) == {
        "stage",
        "return_code",
        "category",
        "stdout_status",
        "stderr_status",
        "log_status",
        "classification_status",
    }
    assert fields["stage"] == "pg_ctl_start"
    assert -64 <= int(fields["return_code"]) <= 255
    assert fields["category"] in {category for _, category in CATEGORIES} | {"unknown"}
    for source in ("stdout_status", "stderr_status"):
        assert fields[source] in {"empty", "usable", "oversized", "invalid"}
    assert fields["log_status"] in {
        "available",
        "missing",
        "unsafe",
        "unreadable",
        "oversized",
        "changed",
    }
    assert fields["classification_status"] in {"no_match", "single_match", "multiple_matches"}
    return fields


def result(code=1, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize("message,category", CATEGORIES)
@pytest.mark.parametrize("source", ["stdout", "stderr", "log"])
def test_startup_categories_have_only_fixed_sanitized_output(
    monkeypatch, message, category, source
):
    data = result()
    log = b""
    if source == "log":
        log = message
    else:
        setattr(data, source, message)
    monkeypatch.setattr(
        guard,
        "_safe_startup_log",
        Mock(return_value=("available", log) if log is not None else ("missing", None)),
    )
    assert diagnostic_fields(guard._startup_diagnostic(data, DIAGNOSTIC_DIR, None)) == dict(
        stage="pg_ctl_start",
        return_code="1",
        category=category,
        stdout_status="usable" if source == "stdout" else "empty",
        stderr_status="usable" if source == "stderr" else "empty",
        log_status="available",
        classification_status="single_match",
    )


@pytest.mark.parametrize(
    "log",
    [
        None,
        b"",
        b"FATAL: unclassified failure PRIVATE_VALUE",
        b"FATAL: permission denied\nFATAL: could not create shared memory",
        b"\xff",
    ],
)
def test_missing_unknown_ambiguous_or_malformed_diagnostics_fail_closed(monkeypatch, log):
    monkeypatch.setattr(
        guard,
        "_safe_startup_log",
        Mock(return_value=("available", log) if log is not None else ("missing", None)),
    )
    fields = diagnostic_fields(guard._startup_diagnostic(result(), DIAGNOSTIC_DIR, None))
    assert fields["category"] == "unknown"
    assert fields["classification_status"] == (
        "multiple_matches" if log and b"permission denied" in log else "no_match"
    )


@pytest.mark.parametrize("failure", [PermissionError, OSError, RuntimeError])
def test_unreadable_diagnostics_do_not_hide_safe_stderr_or_expose_exceptions(monkeypatch, failure):
    monkeypatch.setattr(
        guard, "_safe_startup_log", Mock(side_effect=failure("PRIVATE_PATH_OR_SECRET"))
    )
    value = guard._startup_diagnostic(result(stderr=b"permission denied"), DIAGNOSTIC_DIR, None)
    fields = diagnostic_fields(value)
    assert fields["category"] == "permission_denied"
    assert fields["log_status"] == "unreadable"
    assert fields["classification_status"] == "single_match"
    assert "PRIVATE" not in value


@pytest.mark.parametrize("message,category", CATEGORIES)
def test_sensitive_diagnostic_text_is_classified_but_never_echoed(monkeypatch, message, category):
    # All values below are conspicuous fictional markers, never real credentials.
    sensitive = (
        b" PRIVATE_PASSWORD TOKEN PRIVATE_PATH postgresql://fictional:PRIVATE@remote.invalid/db"
    )
    monkeypatch.setattr(
        guard, "_safe_startup_log", Mock(return_value=("available", message + sensitive))
    )
    value = guard._startup_diagnostic(result(), DIAGNOSTIC_DIR, None)
    fields = diagnostic_fields(value)
    assert fields["category"] == category and fields["classification_status"] == "single_match"
    assert all(
        marker not in value for marker in ["PRIVATE", "TOKEN", "postgresql", "remote.invalid"]
    )


@pytest.mark.parametrize("source", ["stdout", "stderr"])
def test_oversized_captured_output_is_not_inspected_or_echoed(monkeypatch, source):
    log = Mock(return_value=("available", b""))
    monkeypatch.setattr(guard, "_safe_startup_log", log)
    data = result()
    setattr(data, source, b"permission denied" + b"PRIVATE" * 2000)
    fields = diagnostic_fields(guard._startup_diagnostic(data, DIAGNOSTIC_DIR, None))
    assert fields["category"] == "unknown" and fields["classification_status"] == "no_match"
    assert fields[source + "_status"] == "oversized"
    log.assert_called_once()


@pytest.mark.parametrize("code", [None, "PRIVATE", True, 1.0, 0, 256, -65, object()])
def test_unexpected_return_codes_use_numeric_sentinel_and_unknown(monkeypatch, code):
    log = Mock(side_effect=AssertionError("Invalid code must not read a log"))
    monkeypatch.setattr(guard, "_safe_startup_log", log)
    fields = diagnostic_fields(guard._startup_diagnostic(result(code=code), DIAGNOSTIC_DIR, None))
    assert fields["return_code"] == "-1" and fields["category"] == "unknown"
    assert fields["classification_status"] == "no_match"
    log.assert_not_called()


@pytest.mark.parametrize("code", [1, 2, 255, -1, -9, -64])
def test_valid_numeric_exit_and_signal_codes_are_preserved(monkeypatch, code):
    monkeypatch.setattr(guard, "_safe_startup_log", Mock(return_value=("available", b"")))
    fields = diagnostic_fields(guard._startup_diagnostic(result(code=code), DIAGNOSTIC_DIR, None))
    assert fields["return_code"] == str(code) and fields["category"] == "unknown"


@pytest.mark.parametrize("value", [None, "PRIVATE", {}, object()])
def test_unexpected_captured_output_types_fail_closed(monkeypatch, value):
    monkeypatch.setattr(guard, "_safe_startup_log", Mock(return_value=("available", b"")))
    fields = diagnostic_fields(
        guard._startup_diagnostic(result(stdout=value), DIAGNOSTIC_DIR, None)
    )
    assert fields["category"] == "unknown" and fields["stdout_status"] == "invalid"


@pytest.fixture
def fake_owned_log(monkeypatch):
    """Mock descriptor syscalls only: no files, sockets or PostgreSQL processes."""
    state = SimpleNamespace(data=b"FATAL: permission denied", descriptors=[], closed=[])
    directory = SimpleNamespace(st_mode=guard.stat.S_IFDIR | 0o700, st_uid=123)

    def metadata():
        return SimpleNamespace(
            st_mode=guard.stat.S_IFREG | 0o600,
            st_uid=123,
            st_nlink=1,
            st_size=len(state.data),
            st_mtime_ns=7,
        )

    state.directory = directory
    state.metadata = metadata

    def opened(path, flags, **kwargs):
        state.descriptors.append((path, flags, kwargs))
        return len(state.descriptors) + 10

    def fstat(fd):
        return directory if fd == 12 else state.metadata()

    read = Mock(side_effect=lambda fd, size: state.data[:size])
    monkeypatch.setattr(guard.os, "open", Mock(side_effect=opened))
    monkeypatch.setattr(guard.os, "fstat", Mock(side_effect=fstat))
    monkeypatch.setattr(guard.os, "read", read)
    monkeypatch.setattr(guard.os, "close", Mock(side_effect=state.closed.append))
    state.read = read
    return state


def test_safe_log_uses_owned_descriptors_no_follow_and_bounded_read(fake_owned_log):
    state = fake_owned_log
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("available", state.data)
    assert [item[0] for item in state.descriptors] == ["/tmp", DIAGNOSTIC_DIR.name, "postgres.log"]
    assert all(
        flags & guard.os.O_NOFOLLOW and flags & guard.os.O_CLOEXEC
        for _, flags, _ in state.descriptors
    )
    assert state.descriptors[-1][1] & guard.os.O_NONBLOCK
    assert state.descriptors[1][2] == {"dir_fd": 11}
    assert state.descriptors[2][2] == {"dir_fd": 12}
    state.read.assert_called_once_with(13, 8193)
    assert state.closed == [13, 12, 11]


@pytest.mark.parametrize(
    "changes",
    [
        {"st_mode": guard.stat.S_IFDIR | 0o755},
        {"st_uid": 999},
        {"st_mode": guard.stat.S_IFLNK | 0o700},
    ],
)
def test_unsafe_cluster_directory_is_never_read(fake_owned_log, changes):
    state = fake_owned_log
    for name, value in changes.items():
        setattr(state.directory, name, value)
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("unsafe", None)
    state.read.assert_not_called()
    assert state.closed == [12, 11]


@pytest.mark.parametrize(
    "changes",
    [
        {"st_mode": guard.stat.S_IFREG | 0o644},
        {"st_uid": 999},
        {"st_nlink": 2},
        {"st_mode": guard.stat.S_IFIFO | 0o600},
        {"st_mode": guard.stat.S_IFSOCK | 0o600},
        {"st_mode": guard.stat.S_IFLNK | 0o600},
        {"st_size": 8193},
        {"st_size": -1},
    ],
)
def test_unsafe_or_oversized_log_is_never_read(fake_owned_log, changes):
    state = fake_owned_log
    original = state.metadata

    def metadata():
        item = original()
        for name, value in changes.items():
            setattr(item, name, value)
        return item

    state.metadata = metadata
    expected = "oversized" if changes.get("st_size") == 8193 else "unsafe"
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == (expected, None)
    state.read.assert_not_called()
    assert state.closed == [13, 12, 11]


@pytest.mark.parametrize(
    "directory",
    [
        Path("/etc"),
        Path("/tmp/other"),
        Path("relative"),
        "/tmp/dhanvantari-pg-fictional",
        Path("/tmp/dhanvantari-pg-a/../x"),
    ],
)
def test_unowned_path_shapes_fail_before_opening(fake_owned_log, directory):
    assert guard._safe_startup_log(directory, 123) == ("unsafe", None)
    assert fake_owned_log.descriptors == []


@pytest.mark.parametrize("stage", ["parent", "directory", "log", "read", "fstat"])
def test_missing_unreadable_or_symlink_open_failure_closes_owned_descriptors(
    fake_owned_log, monkeypatch, stage
):
    state = fake_owned_log
    original = guard.os.open
    target = {"parent": 0, "directory": 1, "log": 2}.get(stage)

    def opened(path, flags, **kwargs):
        if len(state.descriptors) == target:
            raise OSError("PRIVATE_FAILURE")
        return original(path, flags, **kwargs)

    monkeypatch.setattr(guard.os, "open", opened)
    if stage == "read":
        monkeypatch.setattr(guard.os, "read", Mock(side_effect=PermissionError("PRIVATE")))
    if stage == "fstat":
        monkeypatch.setattr(guard.os, "fstat", Mock(side_effect=OSError("PRIVATE")))
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("unreadable", None)
    assert len(state.closed) == len(state.descriptors)


def test_log_changing_during_read_fails_closed(fake_owned_log):
    state = fake_owned_log
    original = state.metadata
    calls = []

    def changed():
        item = original()
        calls.append(1)
        item.st_mtime_ns += len(calls)
        return item

    state.metadata = changed
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("changed", None)
    assert state.closed == [13, 12, 11]


@pytest.mark.parametrize("status", [3, 0])
def test_startup_failure_classifies_before_existing_cleanup_without_real_operations(
    monkeypatch, status
):
    monkeypatch.setattr(guard.os, "environ", {})
    monkeypatch.setattr(guard.tempfile, "mkdtemp", Mock(return_value=str(DIAGNOSTIC_DIR)))
    monkeypatch.setattr(guard.Path, "mkdir", Mock())
    monkeypatch.setattr(guard.Path, "chmod", Mock())
    monkeypatch.setattr(guard.secrets, "token_urlsafe", Mock(return_value="FICTIONAL-NOT-A-SECRET"))
    monkeypatch.setattr(guard.atexit, "register", Mock())
    monkeypatch.setattr(guard.atexit, "unregister", Mock())
    remove = Mock()
    monkeypatch.setattr(guard.shutil, "rmtree", remove)

    def read_log(directory, uid):
        remove.assert_not_called()
        return "available", b"FATAL: permission denied PRIVATE_VALUE"

    monkeypatch.setattr(guard, "_safe_startup_log", Mock(side_effect=read_log))
    server = object.__new__(guard.DisposablePostgres)
    server.owner = None
    server.directory = server.socket = server.data = None
    server._closed = False
    server._credentials = {}
    server._engines = []
    server._stack = None
    server._start_attempted = False
    server._run = Mock(
        side_effect=[
            result(0, stdout=b"postgres (PostgreSQL) 16.2"),
            result(0),
            result(1, stderr=b"pg_ctl: could not start server"),
            result(0),
            result(status),
        ]
    )
    server._install_guards = Mock(side_effect=AssertionError("No DB connections allowed"))
    with pytest.raises(guard.HarnessError) as error:
        server.start()
    if status == 3:
        fields = diagnostic_fields(str(error.value))
        assert fields == dict(
            stage="pg_ctl_start",
            return_code="1",
            category="permission_denied",
            stdout_status="empty",
            stderr_status="usable",
            log_status="available",
            classification_status="single_match",
        )
        remove.assert_called_once_with(DIAGNOSTIC_DIR)
        assert server._closed and not server._credentials
    else:
        assert "Cluster stop unconfirmed" in str(error.value)
        remove.assert_not_called()
        assert not server._closed
    assert "PRIVATE" not in str(error.value)
    assert [call.args[0] for call in server._run.call_args_list] == [
        "postgres",
        "initdb",
        "pg_ctl",
        "pg_ctl",
        "pg_ctl",
    ]
    server._install_guards.assert_not_called()


@pytest.mark.parametrize("source", ["stdout", "stderr"])
@pytest.mark.parametrize(
    "value,status",
    [
        (b"", "empty"),
        (b"FATAL: unrecognized test message", "usable"),
        (b"x" * 8193, "oversized"),
        (None, "invalid"),
        ("PRIVATE", "invalid"),
        (b"\xff", "invalid"),
    ],
)
def test_each_captured_source_status_is_explicit(monkeypatch, source, value, status):
    monkeypatch.setattr(guard, "_safe_startup_log", Mock(return_value=("available", b"")))
    data = result()
    setattr(data, source, value)
    fields = diagnostic_fields(guard._startup_diagnostic(data, DIAGNOSTIC_DIR, None))
    assert fields[source + "_status"] == status
    assert fields["log_status"] == "available"
    assert fields["classification_status"] == "no_match" and fields["category"] == "unknown"


@pytest.mark.parametrize("status", ["missing", "unsafe", "unreadable", "oversized", "changed"])
def test_recognized_stderr_survives_unavailable_log_without_using_rejected_bytes(
    monkeypatch, status
):
    # Even a faulty reader supplying unsafe text must not cause classification.
    monkeypatch.setattr(
        guard,
        "_safe_startup_log",
        Mock(return_value=(status, b"could not create shared memory PRIVATE_VALUE")),
    )
    fields = diagnostic_fields(
        guard._startup_diagnostic(result(stderr=b"FATAL: permission denied"), DIAGNOSTIC_DIR, None)
    )
    assert fields["log_status"] == status
    assert fields["stderr_status"] == "usable"
    assert fields["category"] == "permission_denied"
    assert fields["classification_status"] == "single_match"


@pytest.mark.parametrize(
    "acquisition,status",
    [
        (None, "unreadable"),
        (("PRIVATE_STATUS", b"PRIVATE"), "unreadable"),
        (("available", None), "unreadable"),
        (("available", b"\xff"), "unreadable"),
        (("available", b"x" * 8193), "oversized"),
    ],
)
def test_malformed_log_reader_results_are_bounded_and_sanitized(monkeypatch, acquisition, status):
    monkeypatch.setattr(guard, "_safe_startup_log", Mock(return_value=acquisition))
    value = guard._startup_diagnostic(result(stderr=b"permission denied"), DIAGNOSTIC_DIR, None)
    fields = diagnostic_fields(value)
    assert fields["log_status"] == status and fields["category"] == "permission_denied"
    assert "PRIVATE" not in value


def test_cross_source_ambiguity_is_never_resolved_arbitrarily(monkeypatch):
    monkeypatch.setattr(
        guard,
        "_safe_startup_log",
        Mock(return_value=("available", b"could not create shared memory segment")),
    )
    fields = diagnostic_fields(
        guard._startup_diagnostic(result(stderr=b"operation not permitted"), DIAGNOSTIC_DIR, None)
    )
    assert fields["classification_status"] == "multiple_matches"
    assert fields["category"] == "unknown"


@pytest.mark.parametrize("source", ["stdout", "stderr"])
def test_unusable_source_does_not_hide_other_safe_sources(monkeypatch, source):
    monkeypatch.setattr(guard, "_safe_startup_log", Mock(return_value=("missing", None)))
    data = result(stdout=b"permission denied", stderr=b"permission denied")
    setattr(data, source, b"could not create shared memory" + b"x" * 8193)
    fields = diagnostic_fields(guard._startup_diagnostic(data, DIAGNOSTIC_DIR, None))
    assert fields[source + "_status"] == "oversized"
    assert (
        fields["category"] == "permission_denied"
        and fields["classification_status"] == "single_match"
    )


@pytest.mark.parametrize(
    "failure,status",
    [
        (FileNotFoundError(guard.errno.ENOENT, "PRIVATE_PATH"), "missing"),
        (PermissionError(guard.errno.EACCES, "PRIVATE_PATH"), "unreadable"),
        (OSError(guard.errno.ELOOP, "PRIVATE_PATH"), "unsafe"),
        (OSError(guard.errno.ENOTDIR, "PRIVATE_PATH"), "unsafe"),
    ],
)
def test_safe_reader_distinguishes_missing_unreadable_and_unsafe_logs(
    fake_owned_log, monkeypatch, failure, status
):
    state = fake_owned_log
    original = guard.os.open

    def opened(path, flags, **kwargs):
        if path == "postgres.log":
            raise failure
        return original(path, flags, **kwargs)

    monkeypatch.setattr(guard.os, "open", opened)
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == (status, None)
    state.read.assert_not_called()
    assert state.closed == [12, 11]


def test_missing_no_follow_flag_is_unsafe_without_fallback(fake_owned_log, monkeypatch):
    monkeypatch.delattr(guard.os, "O_NOFOLLOW")
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("unsafe", None)
    assert fake_owned_log.descriptors == []


def test_malformed_owned_log_is_unreadable_and_closed(fake_owned_log):
    fake_owned_log.data = b"\xff PRIVATE_BYTES"
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("unreadable", None)
    assert fake_owned_log.closed == [13, 12, 11]


def test_empty_owned_log_is_available_without_inventing_a_match(fake_owned_log):
    fake_owned_log.data = b""
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("available", b"")
    assert fake_owned_log.closed == [13, 12, 11]


def test_oversized_owned_log_has_explicit_status_and_no_read(fake_owned_log):
    fake_owned_log.data = b"PRIVATE" * 2000
    assert guard._safe_startup_log(DIAGNOSTIC_DIR, 123) == ("oversized", None)
    fake_owned_log.read.assert_not_called()
    assert fake_owned_log.closed == [13, 12, 11]
