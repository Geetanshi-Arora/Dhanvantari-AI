"""Non-executable namespace launcher proposal. All functions below are pure policies.

No subprocess, socket, namespace, firewall or restriction syscall is executed here.
Generated commands/readback validators are NOT evidence of kernel enforcement.
The existing guard and an additional unconditional block prohibit execution even
if a caller supplies fabricated snapshots or replaces a verifier in a test.
"""

import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from dhanvantari_browser_guard import (
    ROOT,
    BrowserGuardError,
    require_network_boundary,
    require_reviewed_runner,
    validate_environment,
    validate_origin,
)

UNSHARE = "/usr/bin/unshare"
BWRAP = "/usr/bin/bwrap"
IP = "/usr/sbin/ip"
IPTABLES = "/usr/sbin/iptables"
IP6TABLES = "/usr/sbin/ip6tables"
PYTHON = "/workspace/Dhanvantari-AI/.venv/bin/python"
LAUNCHER = str(ROOT / "backend/tests/disposable/dhanvantari_browser_launcher.py")
ROLES = {"server", "browser"}
STAGES = (
    "fresh_user_network_pid_namespaces",
    "close_inherited_descriptors",
    "verify_empty_network",
    "install_ipv4_ipv6_default_drop",
    "configure_loopback_only",
    "bind_owned_ipv4_listener_port_zero",
    "install_exact_port_rules",
    "verify_namespace_and_filter_readback",
    "stage_reviewed_socket_free_bundle",
    "create_separate_role_mount_pid_ipc_capsules",
    "drop_bounding_ambient_inheritable_permitted_effective_caps",
    "apply_no_new_privs_and_reviewed_seccomp",
    "verify_fds_mounts_and_process_restrictions",
    "exec_fixed_server_and_browser",
    "bounded_supervision_and_pid_namespace_teardown",
)
SECCOMP_DENY_REVIEW = (
    "setns",
    "unshare",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "bpf",
    "open_by_handle_at",
    "io_uring_setup",
    "clone3",
)
# A reviewed native filter must also deny these flags in legacy clone(). Merely
# denying unshare/setns does not prevent a workload creating new user namespaces.
CLONE_NAMESPACE_FLAGS = 0x7E020080
# No reviewed bootstrap, server entrypoint, runner or native restriction profile
# is provided by this milestone. No caller-provided hash/boolean can unlock it.


class LauncherBlocked(BrowserGuardError):
    pass


def _reject():
    raise LauncherBlocked("Isolated launcher requirements not satisfied")


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        _reject()
    return value


@dataclass(frozen=True)
class Listener:
    namespace: str
    host: str
    requested_port: int
    assigned_port: int
    fd: int


def approved_origin(listener, expected_origin):
    validate_origin(expected_origin)
    if (
        not isinstance(listener, Listener)
        or listener.host != "127.0.0.1"
        or type(listener.requested_port) is not int
        or listener.requested_port != 0
        or type(listener.fd) is not int
        or listener.fd < 3
        or not isinstance(listener.namespace, str)
        or not re.fullmatch(r"net:\[[0-9]+\]", listener.namespace)
    ):
        _reject()
    origin = f"http://127.0.0.1:{_port(listener.assigned_port)}"
    if origin != expected_origin:
        _reject()
    return origin


def namespace_command():
    return (
        UNSHARE,
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
        "--",
        PYTHON,
        "-I",
        "-S",
        "-B",
        LAUNCHER,
        "--bootstrap",
    )


def validate_namespace_command(command):
    if type(command) is not tuple or command != namespace_command():
        raise LauncherBlocked("Unreviewed namespace command rejected")


def loopback_commands():
    # Only inside a verified fresh namespace, AFTER default-DROP policies.
    # No veth, bridge, external address, default route, DNS or proxy is configured.
    return (
        (IP, "address", "replace", "127.0.0.1/8", "dev", "lo"),
        (IP, "link", "set", "dev", "lo", "up"),
    )


def default_drop_commands():
    return tuple(
        (tool, "-w", "2", "-t", "filter", "-P", chain, "DROP")
        for tool in (IPTABLES, IP6TABLES)
        for chain in ("INPUT", "OUTPUT", "FORWARD")
    )


def port_rules(port):
    port = str(_port(port))
    # Server replies target client ephemeral ports; permit ONLY established replies
    # from the approved source port, not arbitrary NEW connections to other ports.
    rules = []
    for chain, interface in (("OUTPUT", "-o"), ("INPUT", "-i")):
        for direction, states in (("--dport", "NEW,ESTABLISHED"), ("--sport", "ESTABLISHED")):
            rules.append(
                (
                    "-A",
                    chain,
                    interface,
                    "lo",
                    "-s",
                    "127.0.0.1/32",
                    "-d",
                    "127.0.0.1/32",
                    "-p",
                    "tcp",
                    "-m",
                    "tcp",
                    direction,
                    port,
                    "-m",
                    "conntrack",
                    "--ctstate",
                    states,
                    "-j",
                    "ACCEPT",
                )
            )
    return tuple(rules)


def exact_port_commands(port):
    return tuple((IPTABLES, "-w", "2", "-t", "filter", *rule) for rule in port_rules(port))


def verify_filter_readback(source, port, *, ipv6=False):
    """Strict proposed iptables-save filter readback, not a live enforcement probe.

    Normalizes chain counters only. Unknown tables, chains, rule syntax, duplicate
    rules or equivalent-but-unreviewed kernel output fail closed.
    """
    _port(port)
    if not isinstance(source, str) or len(source) > 32768:
        _reject()
    lines = [
        line.strip() for line in source.splitlines() if line.strip() and not line.startswith("#")
    ]
    if not lines or lines[0] != "*filter" or lines[-1] != "COMMIT":
        _reject()
    chains = []
    rules = []
    for line in lines[1:-1]:
        parts = shlex.split(line)
        if line.startswith(":"):
            if (
                len(parts) != 3
                or parts[0] not in (":INPUT", ":OUTPUT", ":FORWARD")
                or parts[1] != "DROP"
            ):
                _reject()
            counts = parts[2]
            if (
                not counts.startswith("[")
                or not counts.endswith("]")
                or len(counts[1:-1].split(":")) != 2
                or not all(value.isdigit() for value in counts[1:-1].split(":"))
            ):
                _reject()
            chains.append(parts[0])
        else:
            rules.append(tuple(parts))
    if set(chains) != {":INPUT", ":OUTPUT", ":FORWARD"} or len(chains) != 3:
        _reject()
    if tuple(rules) != (() if ipv6 else port_rules(port)):
        _reject()


@dataclass(frozen=True)
class NamespaceSnapshot:
    user: str
    network: str
    pid: str
    interfaces: tuple
    addresses: tuple
    ipv4_routes: tuple
    ipv6_routes: tuple
    loopback_up: bool


def verify_namespace(snapshot, parent, *, initialized):
    if any(
        not isinstance(getattr(snapshot, name), str)
        or getattr(snapshot, name) == getattr(parent, name)
        for name in ("user", "network", "pid")
    ):
        _reject()
    if snapshot.interfaces != ("lo",) or type(snapshot.loopback_up) is not bool:
        _reject()
    if initialized:
        if not snapshot.loopback_up or snapshot.addresses != ("127.0.0.1/8",):
            _reject()
        # IPv6 can remain entirely unconfigured; otherwise only ::1 is permitted.
        if any(route not in {"127.0.0.0/8", "127.0.0.1/32"} for route in snapshot.ipv4_routes):
            _reject()
        if any(route != "::1/128" for route in snapshot.ipv6_routes):
            _reject()
    elif snapshot.loopback_up or snapshot.addresses or snapshot.ipv4_routes or snapshot.ipv6_routes:
        _reject()


@dataclass(frozen=True)
class BundleEntry:
    path: str
    kind: str
    links: int = 1


def validate_bundle(entries):
    """Staged COPIES only; never bind the host root, repo, runtime home or /run.

    Runtime/library/code source hashes must be pinned separately in a future review.
    This rejects symlinks rather than attempting to guess safe symlink targets.
    """
    seen = set()
    for entry in entries:
        path = PurePosixPath(entry.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != entry.path
            or not path.parts
            or path.parts[0] not in {"runtime", "code", "libraries", "libraries64", "fonts"}
            or entry.path in seen
            or entry.kind not in {"file", "directory"}
            or type(entry.links) is not int
            or entry.links < 1
            or entry.kind == "file"
            and entry.links != 1
            or any(
                part.startswith(".env")
                or part in {"runtime.env", ".git", "credentials"}
                or part.endswith((".dpapi", ".sock", ".socket"))
                for part in path.parts
            )
        ):
            _reject()
        seen.add(entry.path)
    if not seen:
        _reject()


def capsule_policy(role, *, postgres_socket=None):
    if role not in ROLES or role == "browser" and postgres_socket is not None:
        _reject()
    if role == "server":
        socket_path = PurePosixPath(postgres_socket or "")
        # Preserve the existing PG guard's EXACT generated host path; do not alias /pg.
        if (
            len(socket_path.parts) != 5
            or socket_path.parts[:2] != ("/", "tmp")
            or not socket_path.parts[2].startswith("dhanvantari-pg-")
            or socket_path.parts[3:] != ("socket", ".s.PGSQL.5432")
        ):
            _reject()
    return {
        "new_mount_namespace": True,
        "new_pid_namespace": True,
        "new_ipc_namespace": True,
        "share_only_approved_network_namespace": True,
        "host_root_bind": False,
        "private_proc": True,
        "private_tmp_run_home": True,
        "readonly_staged_bundle": True,
        "postgres_socket": postgres_socket,
        "host_ipc": (),
        "credential_files": (),
        "artifact_exports": (),
    }


@dataclass(frozen=True)
class Descriptor:
    number: int
    kind: str
    namespace: str = ""
    endpoint: str = ""
    sealed: bool = False


def verify_descriptors(role, descriptors, listener, origin):
    approved_origin(listener, origin)
    if role not in ROLES or role == "server" and listener.fd != 3:
        _reject()
    seen = set()
    for item in descriptors:
        if type(item.number) is not int or item.number in seen or item.number < 0:
            _reject()
        seen.add(item.number)
        if item.number in (0, 1, 2) and item.kind == "devnull":
            continue
        if (
            role == "server"
            and item.number == listener.fd
            and item.kind == "owned_listener"
            and item.namespace == listener.namespace
            and item.endpoint == origin
        ):
            continue
        if (
            role == "server"
            and item.kind == "sealed_config"
            and item.sealed is True
            and item.number == 4
        ):
            continue
        _reject()
    expected = {0, 1, 2} if role == "browser" else {0, 1, 2, 3, 4}
    if expected != seen:
        _reject()


def capsule_command(role, origin, staged, *, postgres_socket=None):
    """Proposed empty-root bwrap profile, not an executable launcher.

    FD 5 is a future sealed, reviewed seccomp BPF program, consumed by bwrap;
    before workload execution it must be absent from the descriptor readback.
    Server FD 3 is the newly owned listener and FD 4 a sealed private config.
    The browser inherits none of those three descriptors.
    """
    validate_origin(origin)
    capsule_policy(role, postgres_socket=postgres_socket)
    if not isinstance(staged, str) or not re.fullmatch(
        r"/tmp/dhanvantari-browser-[a-z0-9_]{8}/bundle", staged
    ):
        _reject()
    command = [
        BWRAP,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--dir",
        "/home/test",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--seccomp",
        "5",
    ]
    for source, target in (
        ("runtime", "/runtime"),
        ("code", "/bundle"),
        ("libraries", "/lib"),
        ("libraries64", "/lib64"),
        ("fonts", "/usr/share/fonts"),
    ):
        command.extend(("--ro-bind", staged + "/" + source, target))
    for name, value in child_environment(role).items():
        command.extend(("--setenv", name, value))
    if role == "server":
        command.extend(
            (
                "--dir",
                str(PurePosixPath(postgres_socket).parent),
                "--ro-bind",
                postgres_socket,
                postgres_socket,
                "--preserve-fds",
                "2",
                "--",
                "/runtime/python/bin/python",
                "-I",
                "-S",
                "-B",
                "/bundle/dhanvantari_browser_server.py",
            )
        )
    else:
        command.extend(("--", "/runtime/node/bin/node", "/bundle/patient-backend.cjs", origin))
    return tuple(command)


def validate_capsule_command(command, role, origin, staged, *, postgres_socket=None):
    if type(command) is not tuple or command != capsule_command(
        role, origin, staged, postgres_socket=postgres_socket
    ):
        raise LauncherBlocked("Unreviewed capsule command rejected")


def child_environment(role):
    if role not in ROLES:
        _reject()
    return {"PATH": "/runtime/bin", "HOME": "/home/test", "TMPDIR": "/tmp", "LANG": "C.UTF-8"}


def validate_child_environment(role, environment):
    # Also reuse PG/credential/proxy-related parent rejection, without echoing values.
    validate_environment(environment)
    if type(environment) is not dict or environment != child_environment(role):
        _reject()


def verify_restrictions(snapshot):
    required = {
        "CapInh": 0,
        "CapPrm": 0,
        "CapEff": 0,
        "CapBnd": 0,
        "CapAmb": 0,
        "NoNewPrivs": 1,
        "Seccomp": 2,
        "umask": 0o077,
        "core_limit": 0,
    }
    if not isinstance(snapshot, dict) or any(
        type(snapshot.get(key)) is not int or snapshot[key] != value
        for key, value in required.items()
    ):
        _reject()
    if type(snapshot.get("seccomp_filters")) is not int or snapshot["seccomp_filters"] < 1:
        _reject()


@dataclass(frozen=True)
class LaunchPlan:
    origin: str
    namespace: str
    stages: tuple = field(default=STAGES, init=False)
    executable: bool = field(default=False, init=False)


def prepare_plan(listener, origin):
    return LaunchPlan(approved_origin(listener, origin), listener.namespace)


def execute_plan(plan):
    # These calls and the unconditional final rejection are never bypassed by a
    # plan's metadata. No executable implementation exists in this milestone.
    require_network_boundary()
    require_reviewed_runner()
    raise LauncherBlocked("Isolated launcher is non-executable pending kernel and IPC verification")


if __name__ == "__main__":
    execute_plan(None)
