"""OFFLINE structural checks only; NOT proof of PostgreSQL enforcement or syntax.

No SQL is executed. Alembic upgrade is called only with op.execute mocked. Use
--noconftest to avoid ordinary database fixtures and the Alembic environment.
"""

import importlib.util
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "database/migrations/versions"
BASE = "445d9650365f2b2744e47c054e6b12a555690cc3"
SQL = (VERSIONS / "0005_consent_publication.sql").read_text()
CODE = re.sub(r"--[^\n]*", "", SQL)
TABLES = ("consent_publication_releases", "consent_notice_bindings", "consent_publication_events")
EARLIER = sorted(
    p
    for p in VERSIONS.iterdir()
    if p.name.startswith(("0001_", "0002_", "0003_", "0004_")) and p.suffix in {".py", ".sql"}
)


def table(name):
    start = CODE.index("CREATE TABLE app." + name + " (")
    end = CODE.index("\n);", start)
    return CODE[start:end]


def test_revision_only_executes_its_sql_when_explicitly_called_and_blocks_downgrade():
    spec = importlib.util.spec_from_file_location(
        "publication_migration_offline", VERSIONS / "0005_consent_publication.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("0005", "0004")
    with patch.object(module.op, "execute") as execute:
        module.upgrade()
        execute.assert_called_once_with(SQL)
    with pytest.raises(RuntimeError, match="forward migration"):
        module.downgrade()


@pytest.mark.parametrize("path", EARLIER, ids=lambda path: path.name)
def test_migrations_0001_through_0004_remain_unchanged(path):
    committed = subprocess.check_output(
        ["git", "--no-optional-locks", "show", BASE + ":" + path.relative_to(ROOT).as_posix()],
        cwd=ROOT,
    )
    assert path.read_bytes().replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n")


def test_exact_additive_object_inventory():
    assert set(re.findall(r"CREATE TABLE app\.(\w+)", CODE)) == set(TABLES)
    assert re.findall(r"CREATE ROLE (\w+)", CODE) == ["dhanvantari_notice_publisher"]
    assert re.findall(r"CREATE FUNCTION app\.(\w+)", CODE) == [
        "capture_consent_publication_operator"
    ]
    assert not re.search(
        r"\b(?:DROP|INSERT INTO|UPDATE app\.|DELETE FROM|ALTER DEFAULT PRIVILEGES|CREATE POLICY)\b",
        CODE,
        re.I,
    )
    assert not re.search(r"\b(?:COMMIT|ROLLBACK|BEGIN TRANSACTION)\b", CODE, re.I)
    assert "CREATE OR REPLACE" not in CODE
    assert not re.search(
        r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE)\s+(?:INTO\s+|FROM\s+|TABLE\s+)?app\.", CODE
    )


def test_no_existing_schema_role_function_or_table_is_altered():
    assert set(re.findall(r"ALTER TABLE app\.(\w+)", CODE)) == set(TABLES)
    assert re.findall(r"ALTER FUNCTION app\.(\w+)", CODE) == [
        "capture_consent_publication_operator"
    ]
    assert not re.search(r"\b(?:ALTER ROLE|ALTER SCHEMA|CREATE SCHEMA)\b", CODE)
    assert not re.search(
        r"app\.(?:patients|consents|patient_assignments|patient_operation_keys)\b", CODE
    )
    for name in (
        "register_patient",
        "record_consent_decision",
        "revoke_consent",
        "_registration_notice",
    ):
        assert not re.search(r"(?:CREATE|ALTER|DROP) FUNCTION app\." + name + r"\b", CODE)


def test_publisher_is_dormant_unprivileged_and_gets_schema_usage_only():
    declaration = re.search(r"CREATE ROLE dhanvantari_notice_publisher(.*?);", CODE, re.S).group(1)
    assert set(declaration.split()) == {
        "NOLOGIN",
        "NOINHERIT",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    }
    assert re.findall(r"\bGRANT\s+.*?;", CODE, re.S) == [
        "GRANT USAGE ON SCHEMA app TO dhanvantari_notice_publisher;"
    ]
    assert "WHERE member='dhanvantari_notice_publisher'::regrole" in CODE
    assert "publication.unexpected_publisher_access" in CODE


@pytest.mark.parametrize("name", TABLES)
def test_new_tables_are_owner_only_with_rls_and_no_policies(name):
    assert f"ALTER TABLE app.{name} OWNER TO app_owner;" in CODE
    assert f"ALTER TABLE app.{name} ENABLE ROW LEVEL SECURITY;" in CODE
    assert name in CODE[CODE.index("FOREACH target IN ARRAY") :]
    assert "aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner)))" in CODE
    assert "REVOKE ALL PRIVILEGES ON TABLE app.%I FROM PUBLIC RESTRICT" in CODE
    assert "REVOKE ALL PRIVILEGES ON TABLE app.%I FROM %I RESTRICT" in CODE
    assert "a.grantee<>c.relowner" in CODE
    assert "publication.unexpected_privileges" in CODE


@pytest.mark.parametrize("name", TABLES)
def test_immutable_records_have_update_delete_and_statement_truncate_guards(name):
    assert re.search(
        r"BEFORE UPDATE OR DELETE ON app\."
        + name
        + r"\s+FOR EACH ROW EXECUTE FUNCTION app\.audit_immutable\(\);",
        CODE,
    )
    assert re.search(
        r"BEFORE TRUNCATE ON app\."
        + name
        + r"\s+FOR EACH STATEMENT EXECUTE FUNCTION app\.audit_immutable\(\);",
        CODE,
    )
    assert "GRANT INSERT" not in CODE and "GRANT UPDATE" not in CODE


def test_exact_bytes_and_hash_domains_match_offline_contracts():
    releases = table("consent_publication_releases")
    bindings = table("consent_notice_bindings")
    for field in (
        "canonical_manifest",
        "canonical_evidence",
        "canonical_trust_policy",
        "canonical_reviewer_registry",
    ):
        assert f"{field} bytea NOT NULL" in releases
        assert f"octet_length({field}) BETWEEN 1 AND 2000000" in releases
    assert "manifest_digest=sha256(convert_to('dhanvantari.notice-manifest.v1','UTF8')" in releases
    assert "artifact_digest=sha256(convert_to('dhanvantari.notice-artifact.v1','UTF8')" in bindings
    assert "||decode('00','hex')||canonical_manifest" in releases
    assert "||decode('00','hex')||canonical_artifact" in bindings
    assert "content_hash=sha256(convert_to(notice_text,'UTF8'))" in bindings
    assert (
        "original_approval_evidence_digest=sha256(canonical_original_approval_evidence)" in bindings
    )
    assert "jsonb" not in CODE.lower()
    for digest in (
        "manifest_digest",
        "evidence_bundle_digest",
        "trust_policy_digest",
        "reviewer_registry_digest",
        "content_hash",
        "artifact_digest",
        "original_approval_evidence_digest",
        "event_digest",
    ):
        assert f"octet_length({digest})=32" in CODE


def test_notice_identity_and_text_contracts_are_preserved():
    bindings = table("consent_notice_bindings")
    assert "PRIMARY KEY (purpose,notice_version,language)" in bindings
    assert "purpose IN ('care','uploads','future_ai')" in bindings
    assert "length(notice_version) BETWEEN 1 AND 40" in bindings
    assert "language IN ('en','hi','bn','ta','te','mr','gu','pa')" in bindings
    assert "length(notice_text) BETWEEN 1 AND 65536" in bindings


def test_ledger_chains_and_references_do_not_cascade_or_overwrite():
    releases = table("consent_publication_releases")
    events = table("consent_publication_events")
    assert "release_id varchar(80) NOT NULL UNIQUE" in releases
    assert "manifest_digest bytea NOT NULL UNIQUE" in releases
    assert "ON CONFLICT" not in CODE and "CASCADE" not in CODE
    assert "consent_publication_one_genesis" in CODE
    assert "consent_publication_one_successor" in CODE
    assert "previous_manifest_digest<>manifest_digest" in releases
    assert "FOREIGN KEY (previous_event_id,purpose,notice_version,language)" in events
    assert "UNIQUE (previous_event_id)" in events
    assert "consent_publication_one_initial_event" in CODE
    assert "UNIQUE (release_id,purpose,notice_version,language)" in events


def test_operator_metadata_comes_from_authenticated_session_not_claims():
    body = CODE[
        CODE.index("CREATE FUNCTION app.capture_consent_publication_operator") : CODE.index(
            "ALTER FUNCTION app.capture_consent_publication_operator"
        )
    ]
    assert "RETURNS trigger" in body and "SET search_path=pg_catalog" in body
    assert "SECURITY DEFINER" not in body
    assert "NEW.operator_session_user := session_user;" in body
    assert "WHERE rolname=session_user" in body
    assert "NEW.recorded_at := clock_timestamp();" in body
    assert "request.jwt" not in body and "current_user" not in body
    for name in ("consent_publication_releases", "consent_publication_events"):
        assert f"BEFORE INSERT ON app.{name}" in CODE
        assert "operator_role_oid oid NOT NULL" in table(name)
        assert "request_id uuid NOT NULL" in table(name)
    assert (
        "REVOKE ALL PRIVILEGES ON FUNCTION app.capture_consent_publication_operator() FROM PUBLIC RESTRICT"
        in CODE
    )


def test_schema_foundation_cannot_activate_notices_or_supply_publication_entrypoints():
    assert "publication_enabled boolean NOT NULL DEFAULT false" in CODE
    assert "CONSTRAINT publication_0005_disabled CHECK (NOT publication_enabled)" in CODE
    assert "SELECT FROM app.consent_notice_versions WHERE accepts_new" in CODE
    assert "publication.active_registry_requires_review" in CODE
    assert "SET accepts_new" not in CODE
    assert "reactivation" not in table("consent_publication_events")
    assert "EXECUTE ON FUNCTION" not in CODE
    assert not re.search(r"CREATE FUNCTION app\.(?:publish|activate|retire)", CODE)


def test_privilege_checks_use_matching_postgres_object_types():
    assert "c.relkind IN ('r','p','v','m','f')" in CODE
    assert "c.relkind='S' AND has_sequence_privilege" in CODE
    assert "has_any_column_privilege" in CODE
    assert "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'" in CODE
    assert "has_function_privilege(" in CODE
    assert "has_schema_privilege('dhanvantari_notice_publisher','app','CREATE')" in CODE


def test_documented_trust_limits_and_no_seeds():
    assert "not canonical JSON semantics, signature" in SQL
    assert "shared DB login does not identify a human" in SQL
    assert not re.search(
        r"\b(?:INSERT INTO|COPY app\.|CREATE EXTENSION|ALTER DEFAULT PRIVILEGES)\b", CODE
    )
    assert "VALUES(" not in CODE
