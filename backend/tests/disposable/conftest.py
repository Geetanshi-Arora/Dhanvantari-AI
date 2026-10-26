"""Separate opt-in fixtures: do not load the ordinary backend conftest."""

import os

import pytest
from dhanvantari_pg_guard import DisposablePostgres, no_network, reject_external_environment
from sqlalchemy import text


def pytest_addoption(parser):
    parser.addoption(
        "--run-disposable-postgres",
        action="store_true",
        default=False,
        help="Explicitly authorize starting fresh local test clusters",
    )


@pytest.fixture
def local_pg(request):
    if not request.config.getoption("--run-disposable-postgres"):
        pytest.skip(
            "Disposable PostgreSQL execution requires separate approval and explicit opt-in"
        )
    if request.config.getoption("showlocals"):
        pytest.fail("Local-variable traceback output is disabled for credential safety")
    reject_external_environment(os.environ)
    # Guard Python networking before startup too; libpq gets a separate exact-target guard.
    with no_network():
        server = DisposablePostgres(approved=True)
        try:
            server.start()
            admin = server.engine()
            with admin.begin() as db:
                for role in ("anon", "authenticated", "service_role"):
                    db.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            yield server
        finally:
            server.close()
