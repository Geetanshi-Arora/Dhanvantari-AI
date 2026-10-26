"""Preparation only. Real execution is BLOCKED before allocating any resources.

Python's PostgreSQL/network guards do not confine Node or Chromium. No enforceable
browser egress boundary or reviewed runner exists yet; neither flags nor environment
variables can attest one into existence. Offline tests replace private operations
with fakes solely to exercise teardown paths.
"""

import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dhanvantari_pg_guard import HarnessError, reject_external_environment

ROOT = Path(__file__).resolve().parents[3]
NODE = Path("/opt/codex/runtimes/codex-primary-runtime/dependencies/node/bin/node")
RUNNER = ROOT / "tests/patient-backend.cjs"
START_TIMEOUT = 10
STOP_TIMEOUT = 5
RUN_TIMEOUT = 60
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_CONTEXT = {"service_workers": "block", "accept_downloads": False, "ignore_https_errors": False}


class BrowserGuardError(HarnessError):
    """Only fixed categories; never attach child output, URLs or exception details."""


def require_opt_ins(postgres, browser):
    if postgres is not True or browser is not True:
        raise BrowserGuardError("Both disposable PostgreSQL and browser opt-ins are required")


def validate_environment(environment):
    try:
        reject_external_environment(environment)
    except HarnessError:
        raise BrowserGuardError("Inherited database configuration rejected") from None
    for name in environment:
        if name.startswith(
            ("BROWSER_", "NODE_", "PLAYWRIGHT_", "CHROMIUM_", "UVICORN_", "SUPABASE_", "RUNTIME_")
        ) or name in {
            "HOST",
            "PORT",
            "WEB_CONCURRENCY",
            "ALLOWED_ORIGIN",
            "ENVIRONMENT",
            "SESSION_ENCRYPTION_KEY",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
        }:
            raise BrowserGuardError("Unexpected browser or application environment configuration")


def validate_origin(origin):
    if not isinstance(origin, str) or not re.fullmatch(
        r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", origin
    ):
        raise BrowserGuardError("Exact IPv4 loopback origin required")
    if not 1 <= int(origin.rsplit(":", 1)[1]) <= 65535:
        raise BrowserGuardError("Exact IPv4 loopback origin required")
    return origin


def validate_runner_command(command, origin):
    validate_origin(origin)
    if type(command) is not tuple or command != (str(NODE), str(RUNNER), origin):
        raise BrowserGuardError("Unreviewed browser command rejected")


def browser_context_options():
    return _CONTEXT.copy()


def validate_context_options(options):
    if type(options) is not dict or options != _CONTEXT:
        raise BrowserGuardError("Unsafe browser context configuration rejected")


def allowed_browser_request(origin, url, *, method="GET", websocket=False):
    """Policy predicate for a future runner; NOT an OS/browser network boundary."""
    validate_origin(origin)
    if websocket or not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.fragment or parsed.username or parsed.password or parsed.scheme != "http":
            return False
        if f"{parsed.scheme}://{parsed.netloc}" != origin:
            return False
        if method == "POST":
            return parsed.path in {"/api/v1/auth/login", "/api/v1/auth/logout"} and not parsed.query
        if method != "GET":
            return False
        if parsed.path == "/api/v1/patients":
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            return (
                query.get("limit") == ["20"]
                and set(query) <= {"limit", "cursor"}
                and (
                    "cursor" not in query
                    or len(query["cursor"]) == 1
                    and re.fullmatch(_UUID, query["cursor"][0]) is not None
                )
            )
        if parsed.query:
            return parsed.path == "/login.html" and parsed.query == "reason=expired"
        return (
            parsed.path
            in {
                "/",
                "/index.html",
                "/login.html",
                "/dashboard.html",
                "/register.html",
                "/language.html",
                "/doctor.html",
                "/assets/favicon.svg",
                "/assets/css/styles.css",
                "/assets/js/app.js",
                "/assets/js/auth.js",
                "/assets/js/store.js",
                "/assets/js/patients-api.js",
                "/api/v1/auth/me",
                "/api/v1/auth/csrf",
                "/api/v1/health/live",
                "/api/v1/health/ready",
            }
            or re.fullmatch(r"/api/v1/patients/" + _UUID + r"(?:/registration)?", parsed.path)
            is not None
        )
    except (ValueError, TypeError):
        return False


def require_network_boundary():
    # Deliberately no boolean/config/env override and no Playwright-only fallback.
    raise BrowserGuardError("Browser execution blocked: enforceable network boundary unavailable")


def require_reviewed_runner():
    # The runner is outside Part 1 scope. A future review must pin its source and
    # confinement launcher; merely creating a file at RUNNER must not enable it.
    raise BrowserGuardError("Browser execution blocked: fixed runner has not been reviewed")


def preflight(postgres, browser):
    require_opt_ins(postgres, browser)
    validate_environment(os.environ)
    require_network_boundary()
    require_reviewed_runner()


def uvicorn_options(origin):
    validate_origin(origin)
    return dict(
        host="127.0.0.1",
        port=int(origin.rsplit(":", 1)[1]),
        workers=1,
        reload=False,
        access_log=False,
        log_config=None,
        log_level="critical",
        loop="asyncio",
        http="h11",
        lifespan="on",
        timeout_keep_alive=5,
        timeout_graceful_shutdown=STOP_TIMEOUT,
    )


def _bind_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        host, port = listener.getsockname()
        if host != "127.0.0.1" or type(port) is not int or port <= 0:
            raise BrowserGuardError("Unexpected listener target")
        validate_origin(f"http://127.0.0.1:{port}")
        return listener
    except Exception:
        listener.close()
        raise BrowserGuardError("Loopback listener setup failed") from None


def _make_server(app, origin):
    import uvicorn

    return uvicorn.Server(uvicorn.Config(app, **uvicorn_options(origin)))


def _spawn_runner(origin, directory):
    require_network_boundary()
    require_reviewed_runner()
    command = (str(NODE), str(RUNNER), validate_origin(origin))
    validate_runner_command(command, origin)
    # No inherited credentials, preload hooks, storage exports, shell or arbitrary args.
    environment = {
        "PATH": str(NODE.parent) + ":/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(directory / "home"),
        "TMPDIR": str(directory / "tmp"),
    }
    return subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


class BrowserLifecycle:
    """Owned resources only. start() remains unconditionally blocked in Part 1."""

    def __init__(self, *, postgres_opt_in=False, browser_opt_in=False):
        self.postgres_opt_in, self.browser_opt_in = postgres_opt_in, browser_opt_in
        self.listener = self.server = self.worker = self.process = self.directory = None
        self.origin = None
        self.failed = False
        self.attempted = False

    def start(self, app):
        if self.attempted:
            raise BrowserGuardError("Browser lifecycle cannot be restarted")
        self.attempted = True
        preflight(self.postgres_opt_in, self.browser_opt_in)
        try:
            self.directory = Path(tempfile.mkdtemp(prefix="dhanvantari-browser-", dir="/tmp"))
            self.directory.chmod(0o700)
            for name in ("home", "tmp"):
                (self.directory / name).mkdir(mode=0o700)
            self.listener = _bind_listener()
            host, port = self.listener.getsockname()
            self.origin = validate_origin(f"http://{host}:{port}")
            self.server = _make_server(app, self.origin)

            def serve():
                try:
                    self.server.run(sockets=[self.listener])
                except BaseException:
                    self.failed = True  # Do not print exception/configuration contents.

            self.worker = threading.Thread(target=serve, daemon=True)
            self.worker.start()
            deadline = time.monotonic() + START_TIMEOUT
            while not self.server.started:
                if self.failed or not self.worker.is_alive() or time.monotonic() >= deadline:
                    raise BrowserGuardError("Loopback server startup failed or timed out")
                time.sleep(0.01)
            self.process = _spawn_runner(self.origin, self.directory)
            return self
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            raise BrowserGuardError("Browser lifecycle startup failed") from None

    def wait(self):
        if self.process is None:
            raise BrowserGuardError("Browser runner not started")
        try:
            if self.process.wait(timeout=RUN_TIMEOUT) != 0:
                raise BrowserGuardError("Browser runner failed")
        except subprocess.TimeoutExpired:
            self.close()
            raise BrowserGuardError("Browser runner timed out") from None
        except BrowserGuardError:
            self.close()
            raise
        except Exception:
            self.close()
            raise BrowserGuardError("Browser runner failed") from None

    def close(self):
        failed = False
        if self.process is not None:
            try:
                # Only the private session created with start_new_session=True.
                if self.process.poll() is None and os.getpgid(self.process.pid) != self.process.pid:
                    raise BrowserGuardError("Unexpected runner process group")
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=STOP_TIMEOUT)
                # Parent completion is not sufficient: stop remaining group children too.
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process = None
            except Exception:
                failed = True
        if self.server is not None:
            try:
                self.server.should_exit = True
            except Exception:
                failed = True
        if self.worker is not None:
            try:
                if self.worker.is_alive():
                    self.worker.join(timeout=STOP_TIMEOUT)
                if self.worker.is_alive():
                    self.server.force_exit = True
                    self.worker.join(timeout=STOP_TIMEOUT)
                if self.worker.is_alive():
                    raise BrowserGuardError("Server shutdown unconfirmed")
                self.worker = None
            except Exception:
                failed = True
        if self.listener is not None:
            try:
                self.listener.close()
                self.listener = None
            except Exception:
                failed = True
        if self.directory is not None and not failed:
            try:
                if self.directory.parent != Path("/tmp") or not self.directory.name.startswith(
                    "dhanvantari-browser-"
                ):
                    raise BrowserGuardError("Unexpected browser cleanup directory")
                shutil.rmtree(self.directory)
                self.directory = None
            except Exception:
                failed = True
        if failed:
            raise BrowserGuardError("Browser cleanup unconfirmed; private files retained") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
