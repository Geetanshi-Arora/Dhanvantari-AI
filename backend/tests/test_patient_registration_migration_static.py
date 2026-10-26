"""Offline structural checks, not PostgreSQL execution or concurrency proofs.

Use --noconftest. Do not start a database or import the Alembic environment.
"""

import ast
import importlib.util
import re
import subprocess
from datetime import UTC, datetime, timedelta
from functools import cmp_to_key
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models import clinical, registration  # noqa: F401
from app.models.foundation import Base

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "database/migrations/versions"
SQL = (VERSIONS / "0004_patient_registration.sql").read_text()
# Function bodies are intentionally untagged $$ blocks with static signatures.
FUNCTIONS = {}
for match in re.finditer(r"CREATE FUNCTION app\.(\w+)\(", SQL):
    end = SQL.index("$$;", match.end()) + 3
    FUNCTIONS[match.group(1)] = SQL[match.start() : end]
PUBLIC = {
    "register_patient",
    "record_consent_decision",
    "revoke_consent",
    "patient_registration_status",
}


def canonical_source(source):
    """Ignore only normal Windows checkout line endings, not source changes."""
    return source.replace(b"\r\n", b"\n")


def test_revision_calls_only_reviewed_sql_and_blocks_downgrade():
    spec = importlib.util.spec_from_file_location(
        "registration_migration", VERSIONS / "0004_patient_registration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("0004", "0003")
    with patch.object(module.op, "execute") as execute:
        module.upgrade()
        execute.assert_called_once_with(SQL)
    with pytest.raises(RuntimeError, match="forward migration"):
        module.downgrade()


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in VERSIONS.iterdir()
        if p.name.startswith(("0001_", "0002_", "0003_")) and p.suffix in (".py", ".sql")
    ],
)
def test_previous_migrations_unchanged(path):
    committed = subprocess.check_output(
        [
            "git",
            "--no-optional-locks",
            "show",
            "509a334:" + path.relative_to(ROOT).as_posix(),
        ],
        cwd=ROOT,
    )
    assert canonical_source(path.read_bytes()) == canonical_source(committed)


@pytest.mark.parametrize("checkout", [b"SELECT 1;\n", b"SELECT 1;\r\n"])
def test_migration_comparison_accepts_lf_and_crlf(checkout):
    assert canonical_source(checkout) == canonical_source(b"SELECT 1;\n")


@pytest.mark.parametrize(
    "changed",
    [
        b"SELECT 2;\r\n",
        b"SELECT 1;\r\n-- changed\r\n",
        b"SELECT 1;\r",
    ],
)
def test_migration_comparison_still_detects_source_changes(changed):
    assert canonical_source(changed) != canonical_source(b"SELECT 1;\n")


def test_new_metadata_is_private_and_matches_declared_tables():
    for name in ("consent_notice_versions", "patient_operation_keys"):
        table = Base.metadata.tables["app." + name]
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE app." + name in ddl
        assert "CREATE TABLE app." + name in SQL
        assert "ALTER TABLE app." + name + " ENABLE ROW LEVEL SECURITY" in SQL
        assert "ALTER TABLE app." + name + " OWNER TO app_owner" in SQL
    keys = Base.metadata.tables["app.patient_operation_keys"]
    assert list(keys.primary_key.columns.keys()) == ["actor_id", "operation", "key"]
    assert "octet_length(request_fingerprint) = 32" in {
        str(c.sqltext) for c in keys.constraints if hasattr(c, "sqltext")
    }
    assert list(Base.metadata.tables["app.consent_notice_versions"].primary_key.columns.keys()) == [
        "purpose",
        "notice_version",
        "language",
    ]


def test_no_seeded_notices_memberships_or_changed_clinical_policies():
    assert not re.search(r"INSERT\s+INTO\s+app\.consent_notice_versions", SQL, re.I)
    assert not re.search(r"(CREATE|ALTER|DROP)\s+(ROLE|POLICY)", SQL, re.I)
    assert "ALTER DEFAULT PRIVILEGES" not in SQL
    assert not re.search(r"GRANT\s+(?:INSERT|UPDATE|DELETE|ALL|SELECT)", SQL, re.I)
    assert "public.alembic_version" not in SQL
    assert not re.search(r"\b(?:COMMIT|ROLLBACK|TRUNCATE)\s*;", SQL, re.I)


def test_exact_public_function_allowlist_and_private_helper_acl_coverage():
    grant = SQL[SQL.rindex("GRANT EXECUTE") :]
    assert set(re.findall(r"app\.(\w+)\(", grant)) == PUBLIC
    acl = SQL[SQL.index("FOREACH signature IN ARRAY") : SQL.rindex("GRANT EXECUTE")]
    assert set(re.findall(r"'app\.(\w+)\(", acl)) == set(FUNCTIONS)
    assert "FROM PUBLIC RESTRICT" in acl
    assert "a.grantee<>p.proowner" in acl
    assert "a.grantee<>c.relowner" in acl
    assert "dhanvantari_runtime" in acl
    assert "TO dhanvantari_clinical_actor" in grant


@pytest.mark.parametrize("name", sorted(PUBLIC))
def test_public_boundaries_authorize_and_sanitize_unexpected_failures(name):
    body = FUNCTIONS[name]
    assert "SECURITY DEFINER SET search_path=pg_catalog" in body
    assert "app._registration_actor()" in body
    assert "EXCEPTION WHEN OTHERS THEN" in body
    assert "MESSAGE='registration.operation_failed'" in body
    assert not re.search(r"RAISE\s+(?:EXCEPTION|NOTICE).*%", body)
    if name != "register_patient":
        assert "app._registration_patient(target,actor)" in body


def test_nurse_assignment_and_identity_cannot_be_supplied_in_registration():
    actor = FUNCTIONS["_registration_actor"]
    assert "s.active AND s.role='nurse' FOR SHARE OF s,n" in actor
    assert "JOIN app.nurses" in actor
    assert "current_setting('transaction_isolation')<>'read committed'" in actor
    body = FUNCTIONS["register_patient"]
    assert body.index("app._registration_actor()") < body.index("INSERT INTO app.patients")
    assert "VALUES(target,actor,stamp)" in body
    assert "actor,true,stamp" in body
    validation = FUNCTIONS["_registration_payload"]
    assert "(input - ARRAY['name','age','preferred_language','consents','sex'" in validation
    assert "NOT BETWEEN 18 AND 120" in validation
    assert "registration.care_required" in validation
    assert "count(DISTINCT d->>'purpose')" in validation


def test_length_limit_case_is_parenthesized_inside_plpgsql_if():
    # The PL/pgSQL IF reader must not consume a CASE branch's THEN as the
    # condition terminator. This is a source regression check, not a SQL parser.
    match = re.search(
        r"OR length\(value\) > \(CASE field WHEN 'sex' THEN (\d+) "
        r"WHEN 'contact_reference' THEN (\d+)\s+"
        r"WHEN 'history' THEN (\d+) ELSE (\d+) END\) THEN",
        FUNCTIONS["_registration_payload"],
    )
    assert match is not None
    assert tuple(map(int, match.groups())) == (30, 60, 1000, 500)


def test_registry_is_checked_for_new_decisions_not_for_revocation():
    assert "AND accepts_new FOR SHARE" in FUNCTIONS["_registration_notice"]
    for name in ("register_patient", "record_consent_decision"):
        assert "app._registration_notice(decision)" in FUNCTIONS[name]
        assert FUNCTIONS[name].index("IF replay IS NOT NULL") < FUNCTIONS[name].index(
            "app._registration_notice(decision)"
        )
    assert "app._registration_notice" not in FUNCTIONS["revoke_consent"]
    assert "app._registration_notice" not in FUNCTIONS["patient_registration_status"]


def test_idempotency_unique_claim_serializes_and_does_not_store_request_body():
    body = FUNCTIONS["_registration_claim"]
    assert "ON CONFLICT DO NOTHING" in body
    assert "FOR UPDATE" in body
    assert "sha256(convert_to(payload::text,'UTF8'))" in body
    assert "previous.request_fingerprint<>fingerprint" in body
    assert "previous.result IS NULL" in body
    assert "substring(operation_key::text,15,1)<>'4'" in body
    columns = set(Base.metadata.tables["app.patient_operation_keys"].columns.keys())
    assert columns == {
        "actor_id",
        "operation",
        "key",
        "request_fingerprint",
        "result",
        "created_at",
    }


def test_consent_changes_lock_patient_and_preserve_history():
    lock = FUNCTIONS["_registration_patient"]
    assert "FOR UPDATE" in lock and "FOR SHARE" in lock
    assert "fictional AND age BETWEEN 18 AND 120" in lock
    for name in ("record_consent_decision", "revoke_consent"):
        body = FUNCTIONS[name]
        assert body.index("app._registration_patient") < body.index("UPDATE app.consents")
        assert "app._registration_current_consent(" in body
        assert "WHERE id=latest.id FOR UPDATE" in body
        assert "replay->>'consent_id'" in body and "registration.consent_conflict" in body
    assert "latest.id IS DISTINCT FROM expected_current_id" in FUNCTIONS["record_consent_decision"]
    guard = FUNCTIONS["protect_consent_history"]
    assert "to_jsonb(NEW)-'revoked_at'" in guard
    assert "OLD.revoked_at IS NOT NULL" in guard
    assert "TG_OP='DELETE'" in guard
    assert "NEW.revoked_at<OLD.created_at" in guard


def test_assignment_is_checked_before_patient_lock_and_locked_again_afterward():
    body = FUNCTIONS["_registration_patient"]
    checks = list(
        re.finditer(
            r"PERFORM 1 FROM app\.patient_assignments\s+"
            r"WHERE patient_id=target AND staff_id=actor AND revoked_at IS NULL",
            body,
        )
    )
    patient_lock = body.index("PERFORM 1 FROM app.patients")
    assert len(checks) == 2
    assert checks[0].start() < patient_lock < checks[1].start()
    assert "IF NOT FOUND THEN" in body[checks[0].end() : patient_lock]
    assert "FOR SHARE" not in body[checks[0].end() : patient_lock]
    assert "FOR UPDATE" in body[patient_lock : checks[1].start()]
    assert "FOR SHARE" in body[checks[1].end() :]
    assert "SET LOCAL lock_timeout" in body and "statement_timeout" in body


def test_legacy_precheck_and_selection_share_the_same_ranking():
    precheck = SQL[: SQL.index("CREATE TABLE app.consent_notice_versions")]
    order = "(revoked_at IS NULL) DESC,revoked_at DESC NULLS LAST,created_at DESC"
    assert "LOCK TABLE app.consents IN SHARE ROW EXCLUSIVE MODE" in precheck
    assert "dense_rank() OVER" in precheck
    assert "GROUP BY patient_id,purpose HAVING count(*)>1" in precheck
    assert order in precheck
    selector = FUNCTIONS["_registration_current_consent"]
    assert order in selector.replace("c.", "")
    assert "INTO STRICT selected" in selector
    assert "FETCH FIRST 1 ROW WITH TIES" in selector
    assert "WHEN TOO_MANY_ROWS THEN" in selector
    assert "WHEN NO_DATA_FOUND THEN RETURN NULL" in selector
    assert "registration.ambiguous_history" in selector
    assert "id DESC" not in SQL
    assert "app._registration_current_consent" in FUNCTIONS["_registration_status"]


def test_closure_times_remain_unique_after_legacy_creation_ties():
    for name in ("record_consent_decision", "revoke_consent"):
        body = FUNCTIONS[name]
        assert "max(revoked_at)+interval '1 microsecond'" in body
        assert body.index("app._registration_patient") < body.index("max(revoked_at)")
        assert "UPDATE app.consents SET revoked_at=stamp" in body


def ranked_fixture_candidates(rows):
    """Interpret ONLY the selector's ORDER BY for offline synthetic scenarios.

    This tests ordering intent from the actual SQL text, not PostgreSQL execution,
    row locks, snapshots, or PL/pgSQL exception behavior. Unknown syntax fails.
    """
    clause = re.search(
        r"ORDER BY (.*?)\s+FETCH FIRST", FUNCTIONS["_registration_current_consent"], re.S
    ).group(1)
    terms = []
    for term in clause.split(","):
        match = re.fullmatch(
            r"\s*(\(c\.revoked_at IS NULL\)|c\.(?:revoked_at|created_at)) "
            r"(ASC|DESC)(?: NULLS (FIRST|LAST))?\s*",
            term,
        )
        assert match, "Unsupported offline ordering syntax"
        expression, direction, nulls = match.groups()
        terms.append((expression, direction, nulls or ("FIRST" if direction == "DESC" else "LAST")))

    def compare(left, right):
        for expression, direction, nulls in terms:

            def value(row):
                if expression.startswith("("):
                    return row["revoked_at"] is None
                return row[expression.removeprefix("c.")]

            a, b = value(left), value(right)
            if a is None or b is None:
                result = 0 if a is b else (-1 if a is None else 1)
                if nulls == "LAST":
                    result = -result
            else:
                result = (a > b) - (a < b)
                if direction == "DESC":
                    result = -result
            if result:
                return result
        return 0

    ordered = sorted(rows, key=cmp_to_key(compare))
    return [row for row in ordered if compare(row, ordered[0]) == 0] if ordered else []


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def consent_fixture(identity, granted, closed=None, created=0):
    return dict(
        id=identity,
        granted=granted,
        created_at=T0 + timedelta(seconds=created),
        revoked_at=None if closed is None else T0 + timedelta(seconds=closed),
    )


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], set()),
        ([consent_fixture("grant", True)], {"grant"}),
        # The larger identifier is deliberately historical, not current.
        (
            [consent_fixture("z-old", True, closed=1), consent_fixture("a-active", True)],
            {"a-active"},
        ),
        (
            [consent_fixture("z-old", True, closed=1), consent_fixture("decline", False)],
            {"decline"},
        ),
        (
            [consent_fixture("old", True, closed=1), consent_fixture("revoked", True, closed=2)],
            {"revoked"},
        ),
        # Creation time only breaks ties in closure time; identifiers never do.
        (
            [
                consent_fixture("z-old", True, closed=3),
                consent_fixture("a-new", False, closed=3, created=2),
            ],
            {"a-new"},
        ),
        ([consent_fixture("a", True, closed=1), consent_fixture("z", False, closed=1)], {"a", "z"}),
        (
            [
                consent_fixture("old-a", True, closed=1),
                consent_fixture("old-z", False, closed=1),
                consent_fixture("current", True),
            ],
            {"current"},
        ),
    ],
)
def test_tied_creation_times_active_grants_and_revoked_decision_ordering(rows, expected):
    selected = ranked_fixture_candidates(rows)
    assert {row["id"] for row in selected} == expected
    if len(selected) == 1:
        # Equivalent to the existing helper's grant + non-revoked condition.
        access = any(row["granted"] and row["revoked_at"] is None for row in rows)
        assert access == (selected[0]["granted"] and selected[0]["revoked_at"] is None)
    if len(selected) > 1:
        assert "WHEN TOO_MANY_ROWS THEN" in FUNCTIONS["_registration_current_consent"]


def test_status_has_no_demographic_or_clinical_text_and_audit_is_atomic():
    status = FUNCTIONS["_registration_status"]
    for forbidden in (
        "p.name",
        "p.contact_reference",
        "p.history",
        "p.allergies",
        "p.current_medicines",
    ):
        assert forbidden not in status
    assert "c.granted AND c.revoked_at IS NULL" in status
    for name in PUBLIC:
        assert "app._registration_audit(" in FUNCTIONS[name]
    audit = FUNCTIONS["_registration_audit"]
    assert "INSERT INTO app.audit_logs" in audit
    assert "(id,actor_id,action,outcome,resource_id,request_id)" in audit
    assert "jsonb" not in audit


def test_environment_registers_metadata_and_preserves_precommit_ledger_guard():
    env = (ROOT / "database/migrations/env.py").read_text()
    tree = ast.parse(env)
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.models"
        and any(alias.name == "registration" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert env.index("context.run_migrations()") < env.index("protect_migration_ledger(connection)")
    assert "with context.begin_transaction()" in env
    assert "dont_mutate" in env
