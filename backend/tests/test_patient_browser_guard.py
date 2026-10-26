"""Offline only: no real sockets, subprocesses, servers, browsers or databases."""

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

HERE = Path(__file__).resolve().parent / "disposable"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pg = load("browser_offline_pg", "dhanvantari_pg_guard.py")
with patch.dict(sys.modules, {"dhanvantari_pg_guard": pg}):
    guard = load("browser_offline_guard", "dhanvantari_browser_guard.py")
with patch.dict(sys.modules, {"dhanvantari_pg_guard": pg, "dhanvantari_browser_guard": guard}):
    fixtures = load("browser_offline_fixtures", "conftest.py")
ORIGIN = "http://127.0.0.1:34567"
UID = "20000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def forbid_real_operations(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Real resource operation forbidden offline"))
    monkeypatch.setattr(guard.os, "environ", {})
    monkeypatch.setattr(guard.socket, "socket", forbidden)
    monkeypatch.setattr(guard.subprocess, "Popen", forbidden)
    monkeypatch.setattr(guard.tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(guard.shutil, "rmtree", forbidden)
    monkeypatch.setattr(guard.threading, "Thread", forbidden)
    monkeypatch.setattr(guard.os, "killpg", forbidden)
    monkeypatch.setattr(guard.os, "getpgid", forbidden)
    monkeypatch.setattr(guard, "_make_server", forbidden)
    return forbidden


@pytest.mark.parametrize(
    "postgres,browser", [(False, False), (True, False), (False, True), (1, True), (True, "yes")]
)
def test_missing_explicit_opt_ins_stop_before_any_resources(
    postgres, browser, forbid_real_operations
):
    with pytest.raises(guard.BrowserGuardError, match="Both"):
        guard.BrowserLifecycle(postgres_opt_in=postgres, browser_opt_in=browser).start(object())
    forbid_real_operations.assert_not_called()


def test_flags_and_fake_environment_attestations_cannot_enable_execution(forbid_real_operations):
    guard.require_opt_ins(True, True)
    with pytest.raises(guard.BrowserGuardError, match="enforceable network boundary unavailable"):
        guard.BrowserLifecycle(postgres_opt_in=True, browser_opt_in=True).start(object())
    with pytest.raises(guard.BrowserGuardError, match="runner has not been reviewed"):
        guard.require_reviewed_runner()
    forbid_real_operations.assert_not_called()


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:34567",
        "http://127.0.0.2:34567",
        "http://0.0.0.0:34567",
        "http://[::1]:34567",
        "https://127.0.0.1:34567",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:034567",
        "http://127.0.0.1:34567/",
        "http://user:PRIVATE@127.0.0.1:34567",
        "http://127.0.0.1:34567?x=PRIVATE",
        "http://remote.invalid:34567",
        None,
    ],
)
def test_exact_loopback_origin_only(origin):
    with pytest.raises(guard.BrowserGuardError) as error:
        guard.validate_origin(origin)
    assert "PRIVATE" not in str(error.value)


def test_origin_and_uvicorn_configuration_are_fixed():
    assert guard.validate_origin(ORIGIN) == ORIGIN
    options = guard.uvicorn_options(ORIGIN)
    assert options["host"] == "127.0.0.1" and options["port"] == 34567
    assert options["workers"] == 1 and options["reload"] is False
    assert options["access_log"] is False and options["log_config"] is None
    assert options["lifespan"] == "on" and options["loop"] == "asyncio"
    assert options["timeout_graceful_shutdown"] == guard.STOP_TIMEOUT


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "OTHER_DB_DSN",
        "PGHOST",
        "PGSERVICE",
        "PGOPTIONS",
        "PGPASSWORD",
        "NODE_OPTIONS",
        "NODE_PATH",
        "CHROMIUM_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "UVICORN_HOST",
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
        "RUNTIME_CREDENTIAL_FILE",
        "BROWSER_NETWORK_READY",
        "WEB_CONCURRENCY",
        "ALLOWED_ORIGIN",
        "HOST",
        "PORT",
        "SESSION_ENCRYPTION_KEY",
        "LD_PRELOAD",
    ],
)
def test_database_and_browser_environment_overrides_rejected(name):
    with pytest.raises(guard.BrowserGuardError) as error:
        guard.validate_environment({name: "PRIVATE_MARKER"})
    assert "PRIVATE_MARKER" not in str(error.value)
    assert not error.value.__context__ or error.value.__suppress_context__


def test_benign_parent_environment_does_not_change_browser_settings():
    guard.validate_environment(
        {"PATH": "/fictional", "LANG": "C.UTF-8", "HTTPS_PROXY": "PRIVATE_PLATFORM_PROXY"}
    )
    # Parent platform variables are not passed wholesale to a browser child.
    assert "environ" not in guard._spawn_runner.__code__.co_names


@pytest.mark.parametrize("change", ["shell", "extra", "other_runner", "other_node", "list"])
def test_arbitrary_browser_commands_rejected(change):
    command = (str(guard.NODE), str(guard.RUNNER), ORIGIN)
    assert guard.validate_runner_command(command, ORIGIN) is None
    if change == "shell":
        command = ("sh", "-c", "PRIVATE_COMMAND")
    if change == "extra":
        command += ("--eval=PRIVATE_COMMAND",)
    if change == "other_runner":
        command = (command[0], "/tmp/PRIVATE.cjs", ORIGIN)
    if change == "other_node":
        command = ("node", command[1], ORIGIN)
    if change == "list":
        command = list(command)
    with pytest.raises(guard.BrowserGuardError) as error:
        guard.validate_runner_command(command, ORIGIN)
    assert "PRIVATE" not in str(error.value)


def test_spawning_is_blocked_even_when_fixed_command_is_structurally_valid(forbid_real_operations):
    with pytest.raises(guard.BrowserGuardError, match="network boundary"):
        guard._spawn_runner(ORIGIN, Path("/tmp/dhanvantari-browser-fictional"))
    forbid_real_operations.assert_not_called()


@pytest.mark.parametrize(
    "url,method,websocket",
    [
        ("https://supabase.invalid/auth/v1", "GET", False),
        ("http://127.0.0.1:34568/dashboard.html", "GET", False),
        ("http://localhost:34567/dashboard.html", "GET", False),
        (ORIGIN + "/dashboard.html#PRIVATE", "GET", False),
        ("http://PRIVATE@127.0.0.1:34567/dashboard.html", "GET", False),
        ("ws://127.0.0.1:34567/events", "GET", True),
        (ORIGIN + "/api/v1/patients", "POST", False),
        (ORIGIN + f"/api/v1/patients/{UID}/consents", "POST", False),
        (ORIGIN + "/admin/execute-sql", "GET", False),
        (ORIGIN + "/api/v1/patients?limit=100", "GET", False),
        (ORIGIN + "/api/v1/patients?limit=20&cursor=PRIVATE", "GET", False),
        (ORIGIN + "/api/v1/patients?limit=20&limit=20", "GET", False),
        (ORIGIN + "/api/v1/patients?limit=20&host=PRIVATE", "GET", False),
        (ORIGIN + "/assets/../admin", "GET", False),
        (ORIGIN + "/dashboard.html", "DELETE", False),
    ],
)
def test_external_websocket_admin_and_patient_write_requests_are_denied(url, method, websocket):
    assert guard.allowed_browser_request(ORIGIN, url, method=method, websocket=websocket) is False


@pytest.mark.parametrize(
    "path,method",
    [
        ("/dashboard.html", "GET"),
        ("/assets/js/app.js", "GET"),
        ("/api/v1/auth/login", "POST"),
        ("/api/v1/auth/logout", "POST"),
        ("/api/v1/auth/csrf", "GET"),
        ("/login.html?reason=expired", "GET"),
        ("/api/v1/patients?limit=20", "GET"),
        (f"/api/v1/patients?limit=20&cursor={UID}", "GET"),
        (f"/api/v1/patients/{UID}", "GET"),
        (f"/api/v1/patients/{UID}/registration", "GET"),
    ],
)
def test_policy_allows_only_reviewed_local_reads_and_authentication(path, method):
    assert guard.allowed_browser_request(ORIGIN, ORIGIN + path, method=method)


@pytest.mark.parametrize(
    "extra",
    [
        {"service_workers": "allow"},
        {"storage_state": "PRIVATE.json"},
        {"record_har_path": "PRIVATE.har"},
        {"record_video_dir": "PRIVATE"},
        {"traces_dir": "PRIVATE"},
        {"accept_downloads": True},
        {"ignore_https_errors": True},
    ],
)
def test_service_workers_and_sensitive_artifact_options_are_blocked(extra):
    options = guard.browser_context_options()
    assert options["service_workers"] == "block"
    guard.validate_context_options(options)
    with pytest.raises(guard.BrowserGuardError):
        guard.validate_context_options({**options, **extra})


@pytest.fixture
def fake_lifecycle(monkeypatch):
    class Directory:
        parent = Path("/tmp")
        name = "dhanvantari-browser-fictional"
        chmod = Mock()

        def __truediv__(self, name):
            return SimpleNamespace(mkdir=Mock())

    directory = Directory()
    real_path = Path
    monkeypatch.setattr(
        guard,
        "Path",
        lambda value: (
            directory if str(value) == "/tmp/dhanvantari-browser-fictional" else real_path(value)
        ),
    )
    monkeypatch.setattr(
        guard.tempfile, "mkdtemp", Mock(return_value="/tmp/dhanvantari-browser-fictional")
    )
    monkeypatch.setattr(guard, "require_network_boundary", Mock())
    monkeypatch.setattr(guard, "require_reviewed_runner", Mock())
    listener = Mock()
    listener.getsockname.return_value = ("127.0.0.1", 34567)
    monkeypatch.setattr(guard, "_bind_listener", Mock(return_value=listener))
    server = SimpleNamespace(started=True, should_exit=False, force_exit=False, run=Mock())
    monkeypatch.setattr(guard, "_make_server", Mock(return_value=server))
    worker = Mock()
    state = {"alive": True}
    worker.is_alive.side_effect = lambda: state["alive"]
    worker.join.side_effect = lambda **kwargs: state.update(alive=False)
    monkeypatch.setattr(guard.threading, "Thread", Mock(return_value=worker))
    process = Mock(pid=45678)
    process.poll.return_value = None
    process.wait.return_value = 0
    monkeypatch.setattr(guard, "_spawn_runner", Mock(return_value=process))
    remove = Mock()
    monkeypatch.setattr(guard.shutil, "rmtree", remove)
    kill = Mock()
    monkeypatch.setattr(guard.os, "killpg", kill)
    monkeypatch.setattr(guard.os, "getpgid", Mock(return_value=process.pid))
    return SimpleNamespace(
        lifecycle=guard.BrowserLifecycle(postgres_opt_in=True, browser_opt_in=True),
        directory=directory,
        listener=listener,
        server=server,
        worker=worker,
        state=state,
        process=process,
        remove=remove,
        kill=kill,
    )


def test_fake_success_uses_fixed_configuration_and_bounded_cleanup(fake_lifecycle):
    f = fake_lifecycle
    with f.lifecycle.start(object()):
        assert f.lifecycle.origin == ORIGIN
        f.lifecycle.wait()
    assert f.server.should_exit
    f.worker.join.assert_called_once_with(timeout=guard.STOP_TIMEOUT)
    f.listener.close.assert_called_once()
    f.process.wait.assert_any_call(timeout=guard.RUN_TIMEOUT)
    f.process.wait.assert_any_call(timeout=guard.STOP_TIMEOUT)
    f.remove.assert_called_once_with(f.directory)
    f.kill.assert_any_call(f.process.pid, signal.SIGKILL)
    f.lifecycle.close()  # Idempotent teardown.
    f.remove.assert_called_once()


@pytest.mark.parametrize("stage", ["directory", "socket", "server", "thread", "runner"])
def test_partial_initialization_failure_cleans_owned_resources(fake_lifecycle, monkeypatch, stage):
    f = fake_lifecycle
    operation = {
        "directory": f.directory.chmod,
        "socket": guard._bind_listener,
        "server": guard._make_server,
        "thread": f.worker.start,
        "runner": guard._spawn_runner,
    }[stage]
    operation.side_effect = RuntimeError("PRIVATE_STARTUP_DETAIL")
    if stage == "thread":
        f.state["alive"] = False
    with pytest.raises(guard.BrowserGuardError) as error:
        f.lifecycle.start(object())
    assert "PRIVATE" not in str(error.value)
    f.remove.assert_called_once_with(f.directory)
    if stage not in {"directory", "socket"}:
        f.listener.close.assert_called_once()
    f.lifecycle.close()


@pytest.mark.parametrize("failure", ["dead_worker", "deadline", "server_exception"])
def test_startup_detection_is_bounded_and_sanitized(fake_lifecycle, monkeypatch, failure):
    f = fake_lifecycle
    f.server.started = False
    if failure == "dead_worker":
        f.state["alive"] = False
    elif failure == "deadline":
        monkeypatch.setattr(guard.time, "monotonic", Mock(side_effect=[0, guard.START_TIMEOUT + 1]))
    else:
        f.server.run.side_effect = ValueError("PRIVATE_SERVER_CONFIG")
        f.worker.start.side_effect = lambda: guard.threading.Thread.call_args.kwargs["target"]()
    with pytest.raises(guard.BrowserGuardError) as error:
        f.lifecycle.start(object())
    assert "PRIVATE" not in str(error.value)
    f.remove.assert_called_once()


def test_runner_timeout_stops_resources_without_logging_child_output(fake_lifecycle):
    f = fake_lifecycle
    f.lifecycle.start(object())
    f.process.wait.side_effect = [subprocess.TimeoutExpired("PRIVATE_COMMAND", 60), 0]
    with pytest.raises(guard.BrowserGuardError, match="runner timed out") as error:
        f.lifecycle.wait()
    assert "PRIVATE" not in str(error.value)
    f.listener.close.assert_called_once()
    f.remove.assert_called_once()


def test_runner_shutdown_timeout_escalates_once_with_bounded_waits(fake_lifecycle):
    f = fake_lifecycle
    f.lifecycle.start(object())
    f.process.wait.side_effect = [subprocess.TimeoutExpired("PRIVATE", 5), 0]
    f.lifecycle.close()
    assert [call.kwargs["timeout"] for call in f.process.wait.call_args_list] == [5, 5]
    f.kill.assert_any_call(f.process.pid, signal.SIGKILL)


@pytest.mark.parametrize("failure", ["runner", "worker", "socket"])
def test_unconfirmed_cleanup_retains_directory_and_still_attempts_other_cleanup(
    fake_lifecycle, failure
):
    f = fake_lifecycle
    f.lifecycle.start(object())
    if failure == "runner":
        f.process.wait.side_effect = RuntimeError("PRIVATE")
    elif failure == "worker":
        f.worker.join.side_effect = None
    else:
        f.listener.close.side_effect = RuntimeError("PRIVATE")
    with pytest.raises(guard.BrowserGuardError, match="cleanup unconfirmed") as error:
        f.lifecycle.close()
    assert "PRIVATE" not in str(error.value)
    f.remove.assert_not_called()
    assert f.server.should_exit
    f.listener.close.assert_called_once()
    if failure == "worker":
        assert f.server.force_exit
        assert f.worker.join.call_count == 2


def config(postgres=False, browser=False, showlocals=False):
    return SimpleNamespace(
        getoption=lambda option: {
            "--run-disposable-postgres": postgres,
            "--run-disposable-browser": browser,
            "showlocals": showlocals,
        }[option]
    )


def item(name="test_patient_browser.py", names=()):
    return SimpleNamespace(path=HERE / name, fixturenames=list(names), add_marker=Mock())


@pytest.mark.parametrize(
    "postgres,browser", [(False, False), (True, False), (False, True), (True, True)]
)
def test_collection_gate_precedes_any_database_fixture(postgres, browser, forbid_real_operations):
    browser_test = item()
    regular_test = item("test_patient_database.py", ("local_pg",))
    fixtures.pytest_collection_modifyitems(config(postgres, browser), [browser_test, regular_test])
    browser_test.add_marker.assert_called_once()
    regular_test.add_marker.assert_not_called()
    forbid_real_operations.assert_not_called()


def test_fixture_dependency_gate_cannot_be_bypassed_by_collection(forbid_real_operations):
    request = SimpleNamespace(config=config(True, True), node=item())
    with pytest.raises(pytest.skip.Exception):
        next(fixtures.local_pg.__wrapped__(request))
    forbid_real_operations.assert_not_called()


def test_browser_fixture_names_are_gated_even_in_another_file():
    assert fixtures._browser_case(item("other.py", ("browser_lifecycle", "local_pg")))
    with pytest.raises(guard.BrowserGuardError, match="diagnostics"):
        fixtures._browser_preflight(config(True, True, True))


def test_new_opt_in_is_explicit_and_default_off():
    parser = Mock()
    fixtures.pytest_addoption(parser)
    options = {call.args[0]: call.kwargs for call in parser.addoption.call_args_list}
    assert set(options) == {"--run-disposable-postgres", "--run-disposable-browser"}
    assert all(value["default"] is False for value in options.values())


def test_postgres_guards_are_reused_not_replaced():
    assert guard.reject_external_environment is pg.reject_external_environment
    with pytest.raises(pg.HarnessError):
        pg.validate_connect_parameters({"host": "remote.invalid"}, Path("/tmp/private/socket"), {})


@pytest.mark.parametrize("failure", [None, "bind", "listen", "host"])
def test_listener_binds_os_assigned_loopback_only_and_closes_on_failure(monkeypatch, failure):
    listener = Mock()
    listener.getsockname.return_value = ("127.0.0.1" if failure != "host" else "0.0.0.0", 34567)
    factory = Mock(return_value=listener)
    monkeypatch.setattr(guard.socket, "socket", factory)
    if failure in {"bind", "listen"}:
        getattr(listener, failure).side_effect = RuntimeError("PRIVATE_LISTENER_DETAIL")
    if failure:
        with pytest.raises(guard.BrowserGuardError) as error:
            guard._bind_listener()
        assert "PRIVATE" not in str(error.value)
        listener.close.assert_called_once()
    else:
        assert guard._bind_listener() is listener
        listener.close.assert_not_called()
    listener.bind.assert_called_once_with(("127.0.0.1", 0))


def test_future_spawn_contract_has_no_shell_inheritance_or_diagnostic_exports(monkeypatch):
    # This inspects kwargs through a Mock, never launching Node or any child.
    monkeypatch.setattr(guard, "require_network_boundary", Mock())
    monkeypatch.setattr(guard, "require_reviewed_runner", Mock())
    process = object()
    spawn = Mock(return_value=process)
    monkeypatch.setattr(guard.subprocess, "Popen", spawn)
    monkeypatch.setattr(guard.os, "environ", {"PRIVATE_PLATFORM_VALUE": "PRIVATE_SECRET"})
    directory = Path("/tmp/dhanvantari-browser-fictional")
    assert guard._spawn_runner(ORIGIN, directory) is process
    args, kwargs = spawn.call_args
    assert args == ((str(guard.NODE), str(guard.RUNNER), ORIGIN),)
    assert kwargs["shell"] is False and kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert all(kwargs[name] == subprocess.DEVNULL for name in ("stdin", "stdout", "stderr"))
    assert set(kwargs["env"]) == {"PATH", "LANG", "HOME", "TMPDIR"}
    assert "PRIVATE_SECRET" not in str(kwargs)
    assert kwargs["env"]["HOME"] == str(directory / "home")


def test_runner_nonzero_exit_also_tears_down_server(fake_lifecycle):
    f = fake_lifecycle
    f.lifecycle.start(object())
    f.process.wait.side_effect = [3, 0]
    with pytest.raises(guard.BrowserGuardError, match="runner failed"):
        f.lifecycle.wait()
    f.listener.close.assert_called_once()
    f.remove.assert_called_once()


def test_foreign_process_group_is_not_signalled(fake_lifecycle, monkeypatch):
    f = fake_lifecycle
    f.lifecycle.start(object())
    monkeypatch.setattr(guard.os, "getpgid", Mock(return_value=99999))
    with pytest.raises(guard.BrowserGuardError, match="cleanup unconfirmed"):
        f.lifecycle.close()
    f.kill.assert_not_called()
    f.remove.assert_not_called()
    f.listener.close.assert_called_once()


def test_interruption_during_startup_still_cleans_partial_resources(fake_lifecycle):
    f = fake_lifecycle
    f.state["alive"] = False
    f.worker.start.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        f.lifecycle.start(object())
    f.listener.close.assert_called_once()
    f.remove.assert_called_once()
