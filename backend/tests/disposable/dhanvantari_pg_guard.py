"""Opt-in Linux test cluster; importing this module never starts PostgreSQL.

Only a freshly generated private Unix socket is permitted. No operator .env,
external URL, DNS lookup, TCP connection, existing cluster or Docker is used.
"""

import atexit
import importlib.util
import logging
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.adapt import AdaptersMap
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import URL, create_engine
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
DB_ENV_NAMES = {"DATABASE_URL", "MIGRATION_DATABASE_URL", "SQLALCHEMY_DATABASE_URI"}


class HarnessError(RuntimeError):
    """Fixed messages only; never attach a URL or underlying exception."""


def reject_external_environment(environment, expected_migration_url=None):
    for name in environment:
        sensitive = (
            name.startswith("PG")
            or name in DB_ENV_NAMES
            or name.endswith(("_DATABASE_URL", "_DB_URL", "_DB_DSN"))
        )
        if sensitive:
            if (
                name == "MIGRATION_DATABASE_URL"
                and expected_migration_url is not None
                and environment[name] == expected_migration_url
            ):
                continue
            raise HarnessError("Unexpected database environment configuration")


def validate_target(url, socket_directory, credentials):
    try:
        parsed = make_url(url) if isinstance(url, str) else url
        valid = (
            isinstance(parsed, URL)
            and parsed.drivername == "postgresql+psycopg"
            and parsed.host is None
            and parsed.port is None
            and parsed.database == "postgres"
            and dict(parsed.query) == {"host": str(socket_directory), "port": "5432"}
            and parsed.username in credentials
            and secrets.compare_digest(parsed.password or "", credentials[parsed.username])
        )
    except Exception:
        valid = False
    if not valid:
        raise HarnessError("Non-disposable database target rejected")
    return parsed


def validate_connect_parameters(parameters, socket_directory, credentials):
    allowed = {"host", "port", "dbname", "user", "password", "connect_timeout"}
    try:
        valid = (
            not set(parameters) - allowed
            and parameters.get("host") == str(socket_directory)
            and str(parameters.get("port")) == "5432"
            and parameters.get("dbname") == "postgres"
            and parameters.get("user") in credentials
            and secrets.compare_digest(
                parameters.get("password", ""), credentials[parameters["user"]]
            )
            and str(parameters.get("connect_timeout", "5")) == "5"
        )
    except Exception:
        valid = False
    if not valid:
        raise HarnessError("Non-disposable database connection rejected")


@contextmanager
def no_network():
    """Defense in depth: Python networking is disabled, including DNS."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def blocked(*args, **kwargs):
        raise HarnessError("Network access disabled for disposable tests")

    def connect(sock, address):
        if sock.family != socket.AF_UNIX:
            return blocked()
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family != socket.AF_UNIX:
            return blocked()
        return original_connect_ex(sock, address)

    with (
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", connect_ex),
        patch.object(socket, "create_connection", blocked),
        patch.object(socket, "getaddrinfo", blocked),
    ):
        yield


class DisposablePostgres:
    """Construct only after explicit pytest opt-in; start() is separately invoked."""

    def __init__(self, *, approved=False):
        if approved is not True:
            raise HarnessError("Explicit disposable-test execution approval required")
        reject_external_environment(os.environ)
        if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
            raise HarnessError("This harness requires Linux Unix sockets")
        spec = importlib.util.find_spec("pgserver")
        if spec is None or spec.origin is None:
            raise HarnessError("Bundled PostgreSQL tools unavailable")
        self.bin = Path(spec.origin).parent / "pginstall/bin"
        self.owner = None
        if os.geteuid() == 0:
            try:
                self.owner = pwd.getpwnam("pgserver")
            except KeyError:
                raise HarnessError("Existing unprivileged pgserver OS user required") from None
        self.directory = None
        self.socket = None
        self.data = None
        self._credentials = {}
        self._engines = []
        self._start_attempted = False
        self._closed = False
        self._stack = None

    def __repr__(self):
        return "<DisposablePostgres: private local test cluster>"

    def _run(self, tool, arguments, *, input_data=None, timeout=30):
        identity = {}
        if self.owner is not None:
            identity = dict(user=self.owner.pw_uid, group=self.owner.pw_gid, extra_groups=[])
        try:
            return subprocess.run(
                [str(self.bin / tool), *arguments],
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env={"PATH": str(self.bin) + ":/usr/bin:/bin", "LANG": "C.UTF-8"},
                **identity,
            )
        except Exception:
            raise HarnessError("Disposable PostgreSQL tool operation failed") from None

    def start(self):
        reject_external_environment(os.environ)
        if self.directory is not None:
            raise HarnessError("Cluster start may only be attempted once")
        result = self._run("postgres", ["--version"])
        if result.returncode or result.stdout.strip() != b"postgres (PostgreSQL) 16.2":
            raise HarnessError("Expected bundled PostgreSQL 16.2")
        self.directory = Path(tempfile.mkdtemp(prefix="dhanvantari-pg-", dir="/tmp"))
        self.socket = self.directory / "socket"
        self.data = self.directory / "data"
        self.socket.mkdir(mode=0o700)
        if self.owner is not None:
            for path in (self.directory, self.socket):
                os.chown(path, self.owner.pw_uid, self.owner.pw_gid)
        self.directory.chmod(0o700)
        atexit.register(self.close)
        try:
            # Generated only during approved execution; never printed or written to .env.
            self._credentials = {
                name: secrets.token_urlsafe(48) for name in ("postgres", "dhanvantari_runtime")
            }
            result = self._run(
                "initdb",
                [
                    "-D",
                    str(self.data),
                    "-U",
                    "postgres",
                    "--encoding=UTF8",
                    "--auth-local=scram-sha-256",
                    "--auth-host=reject",
                    "--pwfile=/dev/stdin",
                ],
                input_data=(self._credentials["postgres"] + "\n").encode(),
            )
            if result.returncode:
                raise HarnessError("Disposable cluster initialization failed")
            options = (
                f"-h '' -k {self.socket} -p 5432 -c unix_socket_permissions=0700 "
                "-c statement_timeout=10000 -c lock_timeout=3000 "
                "-c idle_in_transaction_session_timeout=10000 "
                "-c log_statement=none -c log_min_error_statement=panic "
                "-c log_parameter_max_length=0 -c log_parameter_max_length_on_error=0"
            )
            self._start_attempted = True
            result = self._run(
                "pg_ctl",
                [
                    "-D",
                    str(self.data),
                    "-w",
                    "-t",
                    "20",
                    "-l",
                    str(self.directory / "postgres.log"),
                    "-o",
                    options,
                    "start",
                ],
            )
            if result.returncode:
                raise HarnessError("Disposable cluster startup failed")
            self._install_guards()
            return self
        except BaseException:
            self.close()
            raise

    def _install_guards(self):
        self._stack = ExitStack()
        self._stack.enter_context(no_network())
        original_connect = psycopg.connect

        def connect(conninfo="", **kwargs):
            reject_external_environment(
                os.environ, self.url().render_as_string(hide_password=False)
            )
            # SQLAlchemy supplies this Python-only adaptation map. It is not a
            # libpq setting and must never enter target parsing or its allowlist.
            # Accept no other AdaptContext objects or arbitrary keyword options.
            python_options = {}
            if "context" in kwargs:
                context = kwargs.pop("context")
                if type(context) is not AdaptersMap:
                    raise HarnessError("Invalid disposable adapter context")
                python_options["context"] = context
            try:
                parameters = conninfo_to_dict(conninfo, **kwargs)
            except Exception:
                raise HarnessError("Invalid disposable connection parameters") from None
            parameters.setdefault("connect_timeout", "5")
            validate_connect_parameters(parameters, self.socket, self._credentials)
            return original_connect(**parameters, **python_options)

        def async_blocked(*args, **kwargs):
            raise HarnessError("Async connections disabled in disposable harness")

        self._stack.enter_context(patch.object(psycopg, "connect", connect))
        self._stack.enter_context(
            patch.object(
                psycopg.Connection, "connect", classmethod(lambda cls, *a, **kw: connect(*a, **kw))
            )
        )
        self._stack.enter_context(patch.object(psycopg.AsyncConnection, "connect", async_blocked))
        # Alembic must never load the operator's backend/.env, even partially.
        self._stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        # All engines created by migration env.py must use our exact target.
        self._stack.enter_context(patch("sqlalchemy.create_engine", self.engine))
        for name in ("sqlalchemy.engine", "psycopg", "alembic"):
            logger = logging.getLogger(name)
            self._stack.enter_context(patch.object(logger, "handlers", [logging.NullHandler()]))
            self._stack.enter_context(patch.object(logger, "propagate", False))

    def url(self, role="postgres"):
        if self.socket is None or role not in self._credentials:
            raise HarnessError("Disposable target not initialized")
        return URL.create(
            "postgresql+psycopg",
            username=role,
            password=self._credentials[role],
            database="postgres",
            query={"host": str(self.socket), "port": "5432"},
        )

    def engine(self, url=None, **kwargs):
        url = validate_target(self.url() if url is None else url, self.socket, self._credentials)
        if set(kwargs) - {"hide_parameters"}:
            raise HarnessError("Unexpected engine configuration")
        engine = create_engine(
            url, hide_parameters=True, echo=False, connect_args={"connect_timeout": 5}
        )
        self._engines.append(engine)
        return engine

    def migrate(self, revision):
        if revision not in {"0003", "0004"}:
            raise HarnessError("Unreviewed migration target")
        reject_external_environment(os.environ)
        url = self.url().render_as_string(hide_password=False)
        cfg = Config(str(ROOT / "database/alembic.ini"))
        try:
            with patch.dict(os.environ, {"MIGRATION_DATABASE_URL": url}):
                command.upgrade(cfg, revision)
        except Exception:
            raise HarnessError("Disposable migration failed; do not use remote fallback") from None

    def create_runtime(self):
        # DDL passwords cannot use ordinary SQL bind parameters. Render with
        # psycopg Literal, use no engine logger, and suppress raw DDL errors.
        try:
            with psycopg.connect(
                **conninfo_to_dict(
                    self.url()
                    .render_as_string(hide_password=False)
                    .replace("postgresql+psycopg://", "postgresql://", 1)
                )
            ) as db:
                db.execute(
                    psycopg.sql.SQL(
                        "CREATE ROLE dhanvantari_runtime LOGIN INHERIT NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(psycopg.sql.Literal(self._credentials["dhanvantari_runtime"]))
                )
                db.execute("GRANT dhanvantari_api TO dhanvantari_runtime")
        except Exception:
            raise HarnessError("Disposable runtime role setup failed") from None

    def close(self):
        if self._closed:
            return
        try:
            # A failed engine disposal must never skip server shutdown.
            for engine in self._engines:
                try:
                    engine.dispose()
                except Exception:
                    pass
            if self._start_attempted:
                self._run("pg_ctl", ["-D", str(self.data), "-w", "-t", "20", "-m", "fast", "stop"])
                status = self._run("pg_ctl", ["-D", str(self.data), "status"])
                if status.returncode != 3:
                    raise HarnessError("Cluster stop unconfirmed; private directory retained")
            if self.directory is not None:
                if self.directory.parent != Path("/tmp") or not self.directory.name.startswith(
                    "dhanvantari-pg-"
                ):
                    raise HarnessError("Unexpected cleanup directory")
                shutil.rmtree(self.directory)
            self._credentials.clear()
            self._closed = True
            atexit.unregister(self.close)
        finally:
            if self._stack is not None:
                self._stack.close()
                self._stack = None
