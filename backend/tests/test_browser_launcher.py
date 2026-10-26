"""Pure/offline launcher policies: no namespaces, processes, sockets or SQL."""

import ast
import importlib.util
import shlex
import socket
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

HERE = Path(__file__).resolve().parent / "disposable"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


pg = load("launcher_pg_offline", "dhanvantari_pg_guard.py")
with patch.dict(sys.modules, {"dhanvantari_pg_guard": pg}):
    guard = load("launcher_guard_offline", "dhanvantari_browser_guard.py")
with patch.dict(sys.modules, {"dhanvantari_browser_guard": guard}):
    launcher = load("launcher_offline", "dhanvantari_browser_launcher.py")
ORIGIN = "http://127.0.0.1:34567"
LISTENER = launcher.Listener("net:[222]", "127.0.0.1", 0, 34567, 3)
STAGED = "/tmp/dhanvantari-browser-abcdefgh/bundle"
PG_SOCKET = "/tmp/dhanvantari-pg-abcdefgh/socket/.s.PGSQL.5432"


@pytest.fixture(autouse=True)
def no_real_operations(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Real operation forbidden during offline checks"))
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    return forbidden


def test_execution_blockers_are_preserved_even_for_a_complete_plan(no_real_operations):
    plan = launcher.prepare_plan(LISTENER, ORIGIN)
    assert plan.executable is False
    with pytest.raises(FrozenInstanceError):
        plan.executable = True
    with pytest.raises(guard.BrowserGuardError, match="network boundary"):
        launcher.execute_plan(plan)
    no_real_operations.assert_not_called()


def test_fabricated_attestations_cannot_unlock_execution(monkeypatch, no_real_operations):
    monkeypatch.setattr(launcher, "require_network_boundary", Mock())
    monkeypatch.setattr(launcher, "require_reviewed_runner", Mock())
    with pytest.raises(launcher.LauncherBlocked, match="non-executable"):
        launcher.execute_plan(launcher.prepare_plan(LISTENER, ORIGIN))
    no_real_operations.assert_not_called()


@pytest.mark.parametrize(
    "command", [("sh", "-c", "PRIVATE"), ("/tmp/unshare",), (), ["/usr/bin/unshare"]]
)
def test_arbitrary_namespace_commands_rejected(command):
    with pytest.raises(launcher.LauncherBlocked) as error:
        launcher.validate_namespace_command(command)
    assert "PRIVATE" not in str(error.value)


def test_namespace_command_uses_fixed_paths_and_terminates_pid_namespace_child():
    command = launcher.namespace_command()
    launcher.validate_namespace_command(command)
    assert command[0] == "/usr/bin/unshare"
    assert all(
        flag in command
        for flag in (
            "--user",
            "--map-root-user",
            "--net",
            "--pid",
            "--fork",
            "--kill-child=SIGKILL",
        )
    )
    assert command[-1] == "--bootstrap"
    with pytest.raises(launcher.LauncherBlocked):
        launcher.validate_namespace_command(command + ("--eval=PRIVATE",))


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "localhost"},
        {"host": "0.0.0.0"},
        {"requested_port": 34567},
        {"assigned_port": 34568},
        {"fd": 2},
        {"namespace": "net:[parent]"},
    ],
)
def test_origin_must_match_owned_listener_bound_at_port_zero(changes):
    with pytest.raises(guard.BrowserGuardError):
        launcher.prepare_plan(replace(LISTENER, **changes), ORIGIN)


@pytest.mark.parametrize("port", [0, -1, 65536, True, "34567", None])
def test_invalid_ports_cannot_build_firewall_rules(port):
    with pytest.raises(launcher.LauncherBlocked):
        launcher.port_rules(port)


def test_loopback_initialization_has_no_external_interface_or_route_commands():
    commands = launcher.loopback_commands()
    assert commands == (
        ("/usr/sbin/ip", "address", "replace", "127.0.0.1/8", "dev", "lo"),
        ("/usr/sbin/ip", "link", "set", "dev", "lo", "up"),
    )
    assert not any(
        word in {"route", "veth", "bridge", "default"} for command in commands for word in command
    )
    assert len(launcher.default_drop_commands()) == 6
    assert all(command[-1] == "DROP" for command in launcher.default_drop_commands())


def test_exact_port_policy_allows_only_new_requests_and_established_replies():
    rules = launcher.port_rules(34567)
    assert len(rules) == 4
    for rule in rules:
        assert rule[rule.index("-s") + 1] == "127.0.0.1/32"
        assert rule[rule.index("-d") + 1] == "127.0.0.1/32"
        assert rule[rule.index("-p") + 1] == "tcp"
        if "--dport" in rule:
            assert rule[rule.index("--dport") + 1] == "34567"
            assert rule[rule.index("--ctstate") + 1] == "NEW,ESTABLISHED"
        else:
            assert rule[rule.index("--sport") + 1] == "34567"
            assert rule[rule.index("--ctstate") + 1] == "ESTABLISHED"
    assert all(command[0] == launcher.IPTABLES for command in launcher.exact_port_commands(34567))


def saved_filter(port=34567, ipv6=False):
    rules = () if ipv6 else launcher.port_rules(port)
    return "\n".join(
        [
            "# fixture-only readback",
            "*filter",
            ":INPUT DROP [1:100]",
            ":OUTPUT DROP [0:0]",
            ":FORWARD DROP [0:0]",
            *(shlex.join(rule) for rule in rules),
            "COMMIT",
        ]
    )


def test_strict_filter_readback_accepts_only_complete_expected_policy():
    launcher.verify_filter_readback(saved_filter(), 34567)
    launcher.verify_filter_readback(saved_filter(ipv6=True), 34567, ipv6=True)


@pytest.mark.parametrize(
    "mutation",
    [
        "other_port",
        "accept_default",
        "missing_chain",
        "extra_rule",
        "ipv6_accept",
        "missing_commit",
        "new_replies",
    ],
)
def test_filter_installation_or_readback_mismatch_fails_closed(mutation):
    source = saved_filter()
    ipv6 = False
    if mutation == "other_port":
        source = saved_filter(34568)
    elif mutation == "accept_default":
        source = source.replace(":OUTPUT DROP", ":OUTPUT ACCEPT")
    elif mutation == "missing_chain":
        source = source.replace(":FORWARD DROP [0:0]\n", "")
    elif mutation == "extra_rule":
        source = source.replace("COMMIT", "-A OUTPUT -j ACCEPT\nCOMMIT")
    elif mutation == "ipv6_accept":
        ipv6 = True
    elif mutation == "missing_commit":
        source = source.replace("COMMIT", "")
    else:
        source = source.replace("--ctstate ESTABLISHED", "--ctstate NEW,ESTABLISHED")
    with pytest.raises(launcher.LauncherBlocked):
        launcher.verify_filter_readback(source, 34567, ipv6=ipv6)


PARENT = launcher.NamespaceSnapshot("user:[1]", "net:[1]", "pid:[1]", ("lo",), (), (), (), False)
FRESH = replace(PARENT, user="user:[2]", network="net:[222]", pid="pid:[2]")
READY = replace(
    FRESH, loopback_up=True, addresses=("127.0.0.1/8",), ipv4_routes=("127.0.0.0/8", "127.0.0.1/32")
)


def test_fresh_and_initialized_namespace_snapshots():
    launcher.verify_namespace(FRESH, PARENT, initialized=False)
    launcher.verify_namespace(READY, PARENT, initialized=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"network": PARENT.network},
        {"pid": PARENT.pid},
        {"user": PARENT.user},
        {"interfaces": ("lo", "eth0")},
        {"addresses": ("192.0.2.1/24",)},
        {"ipv4_routes": ("default",)},
        {"ipv6_routes": ("::/0",)},
        {"loopback_up": False},
    ],
)
def test_missing_namespace_isolation_or_external_routes_are_rejected(changes):
    with pytest.raises(launcher.LauncherBlocked):
        launcher.verify_namespace(replace(READY, **changes), PARENT, initialized=True)


def test_no_setup_can_skip_default_deny_or_restrictions_before_workloads():
    stages = launcher.prepare_plan(LISTENER, ORIGIN).stages
    assert stages.index("install_ipv4_ipv6_default_drop") < stages.index("configure_loopback_only")
    assert stages.index("verify_namespace_and_filter_readback") < stages.index(
        "exec_fixed_server_and_browser"
    )
    assert stages.index("apply_no_new_privs_and_reviewed_seccomp") < stages.index(
        "exec_fixed_server_and_browser"
    )
    assert stages[-1] == "bounded_supervision_and_pid_namespace_teardown"


@pytest.mark.parametrize(
    "entry",
    [
        launcher.BundleEntry("/run/proxy.sock", "socket"),
        launcher.BundleEntry("code/../secret", "file"),
        launcher.BundleEntry("code/.env", "file"),
        launcher.BundleEntry("code/runtime.env", "file"),
        launcher.BundleEntry("code/key.dpapi", "file"),
        launcher.BundleEntry("code/.git/config", "file"),
        launcher.BundleEntry("runtime/escape", "symlink"),
        launcher.BundleEntry("runtime/pipe", "fifo"),
        launcher.BundleEntry("runtime/device", "device"),
        launcher.BundleEntry("runtime/host.sock", "file"),
        launcher.BundleEntry("runtime/hardlink", "file", 2),
        launcher.BundleEntry("home/profile", "file"),
    ],
)
def test_ipc_proxy_credentials_and_symlink_bundle_entries_are_rejected(entry):
    with pytest.raises(launcher.LauncherBlocked):
        launcher.validate_bundle([entry])


def test_bundle_requires_only_reviewed_copies_and_no_duplicates():
    launcher.validate_bundle(
        [
            launcher.BundleEntry("runtime", "directory", 2),
            launcher.BundleEntry("code/app.py", "file"),
        ]
    )
    with pytest.raises(launcher.LauncherBlocked):
        launcher.validate_bundle([])
    with pytest.raises(launcher.LauncherBlocked):
        launcher.validate_bundle([launcher.BundleEntry("code/app.py", "file")] * 2)


def test_role_capsules_hide_host_ipc_and_keep_pg_socket_server_only():
    browser = launcher.capsule_policy("browser")
    server = launcher.capsule_policy("server", postgres_socket=PG_SOCKET)
    assert browser["postgres_socket"] is None and server["postgres_socket"] == PG_SOCKET
    for policy in (browser, server):
        assert (
            policy["new_mount_namespace"]
            and policy["new_pid_namespace"]
            and policy["new_ipc_namespace"]
        )
        assert policy["private_proc"] and policy["private_tmp_run_home"]
        assert (
            not policy["host_root_bind"]
            and not policy["host_ipc"]
            and not policy["artifact_exports"]
        )
    with pytest.raises(launcher.LauncherBlocked):
        launcher.capsule_policy("browser", postgres_socket=PG_SOCKET)
    with pytest.raises(launcher.LauncherBlocked):
        launcher.capsule_policy("server", postgres_socket="/run/docker.sock")


@pytest.mark.parametrize("role", ["server", "browser"])
def test_capsule_commands_are_fixed_and_use_only_staged_readonly_mounts(role):
    kwargs = {"postgres_socket": PG_SOCKET} if role == "server" else {}
    command = launcher.capsule_command(role, ORIGIN, STAGED, **kwargs)
    launcher.validate_capsule_command(command, role, ORIGIN, STAGED, **kwargs)
    assert command[0] == launcher.BWRAP and "--unshare-pid" in command and "--proc" in command
    assert "--clearenv" in command and "--cap-drop" in command and "--seccomp" in command
    assert "--unshare-net" not in command  # Share ONLY the outer, newly isolated network.
    for index, token in enumerate(command):
        if token == "--ro-bind":
            assert (
                command[index + 1].startswith(STAGED + "/")
                or role == "server"
                and command[index + 1] == PG_SOCKET
            )
    if role == "browser":
        assert PG_SOCKET not in command and "--preserve-fds" not in command
        assert command[-3:] == ("/runtime/node/bin/node", "/bundle/patient-backend.cjs", ORIGIN)
    with pytest.raises(launcher.LauncherBlocked):
        launcher.validate_capsule_command(
            command + ("PRIVATE_ARG",), role, ORIGIN, STAGED, **kwargs
        )


@pytest.mark.parametrize(
    "staged",
    [
        "/",
        "/workspace",
        "/tmp/bundle",
        "/tmp/dhanvantari-browser-abcdefgh/../bundle",
        "/run/browser/bundle",
    ],
)
def test_host_filesystem_cannot_be_supplied_as_bundle(staged):
    with pytest.raises(launcher.LauncherBlocked):
        launcher.capsule_command("browser", ORIGIN, staged)


def descriptors():
    return [launcher.Descriptor(number, "devnull") for number in range(3)]


def test_browser_inherits_no_network_fds_server_only_owns_listener_and_sealed_config():
    launcher.verify_descriptors("browser", descriptors(), LISTENER, ORIGIN)
    launcher.verify_descriptors(
        "server",
        descriptors()
        + [
            launcher.Descriptor(3, "owned_listener", LISTENER.namespace, ORIGIN),
            launcher.Descriptor(4, "sealed_config", sealed=True),
        ],
        LISTENER,
        ORIGIN,
    )


@pytest.mark.parametrize(
    "extra",
    [
        launcher.Descriptor(3, "socket"),
        launcher.Descriptor(3, "pipe"),
        launcher.Descriptor(3, "owned_listener", LISTENER.namespace, ORIGIN),
        launcher.Descriptor(4, "sealed_config", sealed=True),
        launcher.Descriptor(9, "unix_proxy"),
    ],
)
def test_browser_rejects_inherited_proxy_database_or_other_descriptors(extra):
    with pytest.raises(launcher.LauncherBlocked):
        launcher.verify_descriptors("browser", descriptors() + [extra], LISTENER, ORIGIN)


def test_wrong_listener_endpoint_or_unsealed_config_cannot_reach_server():
    for socket_entry, config in [
        (
            launcher.Descriptor(3, "owned_listener", LISTENER.namespace, "http://127.0.0.1:34568"),
            launcher.Descriptor(4, "sealed_config", sealed=True),
        ),
        (
            launcher.Descriptor(3, "owned_listener", LISTENER.namespace, ORIGIN),
            launcher.Descriptor(4, "sealed_config", sealed=False),
        ),
    ]:
        with pytest.raises(launcher.LauncherBlocked):
            launcher.verify_descriptors(
                "server", descriptors() + [socket_entry, config], LISTENER, ORIGIN
            )


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "PGHOST",
        "SUPABASE_URL",
        "NODE_OPTIONS",
        "NODE_PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "BROWSER_MANAGEMENT_SOCKET_PATH",
        "PRIVATE_EXTRA",
    ],
)
def test_minimal_child_environment_rejects_credentials_proxy_and_overrides(name):
    with pytest.raises(guard.BrowserGuardError) as error:
        launcher.validate_child_environment(
            "browser", {**launcher.child_environment("browser"), name: "PRIVATE_MARKER"}
        )
    assert "PRIVATE_MARKER" not in str(error.value)


RESTRICTIONS = {
    "CapInh": 0,
    "CapPrm": 0,
    "CapEff": 0,
    "CapBnd": 0,
    "CapAmb": 0,
    "NoNewPrivs": 1,
    "Seccomp": 2,
    "umask": 0o077,
    "core_limit": 0,
    "seccomp_filters": 1,
}


@pytest.mark.parametrize("name", list(RESTRICTIONS))
def test_failed_capability_or_process_restriction_application_aborts(name):
    launcher.verify_restrictions(RESTRICTIONS)
    wrong = 1 if RESTRICTIONS[name] == 0 else 0
    with pytest.raises(launcher.LauncherBlocked):
        launcher.verify_restrictions({**RESTRICTIONS, name: wrong})


def test_partial_restriction_snapshot_is_not_a_success_attestation():
    with pytest.raises(launcher.LauncherBlocked):
        launcher.verify_restrictions({"NoNewPrivs": 1})
    assert "clone3" in launcher.SECCOMP_DENY_REVIEW and launcher.CLONE_NAMESPACE_FLAGS != 0


def test_launcher_has_no_execution_or_dynamic_command_implementation():
    tree = ast.parse((HERE / "dhanvantari_browser_launcher.py").read_text())
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any(
        isinstance(node, ast.Import)
        and any(alias.name in {"socket", "subprocess", "ctypes"} for alias in node.names)
        for node in imports
    )
    assert launcher.validate_environment is guard.validate_environment
    assert (HERE / "dhanvantari_pg_guard.py").is_file()
