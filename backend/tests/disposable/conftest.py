"""Separate opt-in fixtures: do not load the ordinary backend conftest."""

import os
from pathlib import Path

import pytest
from dhanvantari_browser_guard import BrowserGuardError, preflight
from dhanvantari_pg_guard import DisposablePostgres, no_network, reject_external_environment
from sqlalchemy import text


def pytest_addoption(parser):
    parser.addoption(
        "--run-disposable-postgres",
        action="store_true",
        default=False,
        help="Explicitly authorize starting fresh local test clusters",
    )
    parser.addoption(
        "--run-disposable-browser",
        action="store_true",
        default=False,
        help="Additional opt-in; browser execution remains blocked pending network confinement",
    )


def _browser_case(item):
    return Path(item.path).name == "test_patient_browser.py" or bool(
        {"browser_preflight", "browser_lifecycle"}.intersection(item.fixturenames)
    )


def _browser_preflight(config):
    if config.getoption("showlocals"):
        raise BrowserGuardError("Local-variable diagnostics are forbidden for browser tests")
    preflight(
        config.getoption("--run-disposable-postgres"),
        config.getoption("--run-disposable-browser"),
    )


def pytest_collection_modifyitems(config, items):
    # Skip before dependency fixtures: checking only inside a browser fixture is
    # too late if pytest has already started its local_pg dependency.
    for item in items:
        if _browser_case(item):
            try:
                _browser_preflight(config)
            except BrowserGuardError as error:
                item.add_marker(pytest.mark.skip(reason=str(error)))


@pytest.fixture
def browser_preflight(request):
    try:
        _browser_preflight(request.config)
    except BrowserGuardError as error:
        pytest.skip(str(error))


@pytest.fixture
def local_pg(request):
    if _browser_case(request.node):
        try:
            _browser_preflight(request.config)
        except BrowserGuardError as error:
            pytest.skip(str(error))
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
