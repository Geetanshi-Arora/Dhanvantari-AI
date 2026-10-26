"""Offline structure checks only: never import or invoke integration fixtures."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "backend/tests/disposable/test_patient_api.py"


@pytest.fixture
def tree():
    return ast.parse(SOURCE.read_text())


def function(tree, name):
    return next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def calls(node, name):
    return [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
        and (
            isinstance(item.func, ast.Name)
            and item.func.id == name
            or isinstance(item.func, ast.Attribute)
            and item.func.attr == name
        )
    ]


def test_fixture_requires_guarded_cluster_before_setup(tree):
    fixture = function(tree, "api")
    assert [argument.arg for argument in fixture.args.args] == ["local_pg"]
    assert [call.args[0].value for call in calls(fixture, "migrate")] == ["0004"]
    assert calls(fixture, "create_runtime")
    for call in calls(fixture, "engine"):
        assert isinstance(call.func.value, ast.Name) and call.func.value.id == "local_pg"
    assert calls(fixture, "ExitStack") and calls(fixture, "enter_context")
    assert not calls(tree, "DisposablePostgres") and not calls(tree, "create_engine")


def test_every_integration_test_requires_guarded_api_fixture(tree):
    tests = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    assert len(tests) >= 15
    assert all("api" in [argument.arg for argument in node.args.args] for node in tests)
    # Top-level collection has no DB, migration, HTTP or subprocess calls.
    allowed = {"frozenset", "UUID"}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        assert all(
            isinstance(call.func, ast.Name) and call.func.id in allowed
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )


def test_application_uses_only_injected_runtime_and_fictional_provider(tree):
    fixture = function(tree, "api")
    app = calls(fixture, "create_app")
    assert len(app) == 1
    assert {keyword.arg for keyword in app[0].keywords} == {"engine", "auth"}
    settings = calls(fixture, "Settings")[0]
    fields = {keyword.arg: keyword.value for keyword in settings.keywords}
    assert isinstance(fields["_env_file"], ast.Constant) and fields["_env_file"].value is None
    assert isinstance(fields["supabase_url"], ast.Constant)
    assert fields["supabase_url"].value == "https://fictional.supabase.co"
    assert not calls(tree, "load_dotenv") and not calls(tree, "load_runtime_settings")
    source = SOURCE.read_text()
    assert "dependency_overrides" not in source
    assert "setattr(security" not in source
    assert "runtime.dpapi" not in source


def test_failure_injection_does_not_replace_routes_or_services(tree):
    patches = calls(tree, "setattr")
    assert len(patches) == 1
    assert patches[0].args[1].value == "PatientRegistrationResponse"
    # The real function must run before the intentionally failing response check.
    assert calls(
        function(tree, "test_response_contract_failure_rolls_back_real_function_writes"), "register"
    )


def test_existing_harness_still_protects_remote_targets():
    source = (SOURCE.parent / "conftest.py").read_text()
    assert 'getoption("--run-disposable-postgres")' in source
    assert "reject_external_environment(os.environ)" in source
    assert "with no_network():" in source and "server.close()" in source
    source = (SOURCE.parent / "dhanvantari_pg_guard.py").read_text()
    for protection in (
        "validate_connect_parameters(parameters, self.socket, self._credentials)",
        'patch.object(psycopg, "connect", connect)',
        'patch.object(socket, "getaddrinfo", blocked)',
        '"--auth-host=reject"',
        "-h ''",
        '"context" in kwargs',
        "type(context) is not AdaptersMap",
    ):
        assert protection in source
