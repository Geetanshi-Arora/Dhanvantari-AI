"""Opt-in, fresh guarded PostgreSQL ONLY. Every record is fictional test data.

No notice is legally approved or published. Temporary constraint fixtures remain
inactive. Registry metadata used for regression tests exists only in this cluster.
Unexpected migration errors stop execution with -x; never rewrite migration SQL.
"""

import json
from hashlib import sha256
from uuid import uuid4

import pytest
import test_patient_database as clinical
from dhanvantari_pg_guard import HarnessError
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

TABLES = ("consent_publication_releases", "consent_notice_bindings", "consent_publication_events")
DOMAIN = b"dhanvantari.notice-manifest.v1\x00"
ARTIFACT_DOMAIN = b"dhanvantari.notice-artifact.v1\x00"
NOTICE_TEXT = "FICTIONAL NON-APPROVED CONSTRAINT FIXTURE ONLY. Ω\r\n"


def diagnostic(error):
    """Migration DDL contains no credentials. Return primary DB diagnostic only."""
    pending = [error]
    seen = set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if getattr(item, "sqlstate", None):
            diag = getattr(item, "diag", None)
            return item.sqlstate, getattr(diag, "message_primary", "Database operation failed")
        pending.extend([getattr(item, "orig", None), getattr(item, "__context__", None)])
    return None, "No database diagnostic available"


def migrate(server, revision="0005"):
    try:
        server.migrate(revision)
    except HarnessError as error:
        code, primary = diagnostic(error)
        pytest.fail(f"Migration {revision} failed: SQLSTATE {code}; {primary}", pytrace=False)


@pytest.fixture
def ready(local_pg):
    migrate(local_pg)
    local_pg.create_runtime()
    return local_pg, local_pg.engine(), local_pg.engine(local_pg.url("dhanvantari_runtime"))


def counts(db):
    return tuple(db.scalar(text(f"SELECT count(*) FROM app.{table}")) for table in TABLES)


def release_values(**changes):
    identity = uuid4()
    canonical = json.dumps(
        {"fixture": "NON-APPROVED", "id": str(identity)}, sort_keys=True
    ).encode()
    evidence = b'[{"fixture":"NOT-HUMAN-APPROVAL"}]'
    policy = b'{"fixture":"NOT-A-TRUST-POLICY"}'
    registry = b'{"keys":[],"reviewers":[]}'
    return (
        dict(
            id=identity,
            release_id="FICTIONAL-" + identity.hex,
            canonical_manifest=canonical,
            manifest_digest=sha256(DOMAIN + canonical).digest(),
            previous_manifest_digest=None,
            canonical_evidence=evidence,
            evidence_bundle_digest=sha256(evidence).digest(),
            canonical_trust_policy=policy,
            trust_policy_digest=sha256(policy).digest(),
            canonical_reviewer_registry=registry,
            reviewer_registry_digest=sha256(registry).digest(),
            publication_enabled=False,
            request_id=uuid4(),
        )
        | changes
    )


def insert_release(db, **changes):
    values = release_values(**changes)
    db.execute(
        text(
            "INSERT INTO app.consent_publication_releases "
            "(id,release_id,manifest_digest,previous_manifest_digest,canonical_manifest,canonical_evidence,"
            "evidence_bundle_digest,canonical_trust_policy,trust_policy_digest,canonical_reviewer_registry,"
            "reviewer_registry_digest,publication_enabled,request_id,operator_session_user,operator_role_oid,recorded_at) "
            "VALUES(:id,:release_id,:manifest_digest,:previous_manifest_digest,:canonical_manifest,:canonical_evidence,"
            ":evidence_bundle_digest,:canonical_trust_policy,:trust_policy_digest,:canonical_reviewer_registry,"
            ":reviewer_registry_digest,:publication_enabled,:request_id,'FICTIONAL-SPOOF',0,'2000-01-01T00:00:00Z')"
        ),
        values,
    )
    return values


def binding_values(release):
    canonical = b'{"fixture":"IMMUTABLE-NON-APPROVED-ARTIFACT"}'
    evidence = b'[{"fixture":"NON-APPROVED-ORIGINAL-EVIDENCE"}]'
    return dict(
        purpose="care",
        notice_version="FIXTURE-NOT-APPROVED",
        language="en",
        notice_text=NOTICE_TEXT,
        content_hash=sha256(NOTICE_TEXT.encode()).digest(),
        canonical_artifact=canonical,
        artifact_digest=sha256(ARTIFACT_DOMAIN + canonical).digest(),
        canonical_original_approval_evidence=evidence,
        original_approval_evidence_digest=sha256(evidence).digest(),
        first_release_id=release["id"],
    )


def insert_binding(db, values):
    fields = tuple(values)
    db.execute(
        text(
            "INSERT INTO app.consent_notice_bindings ("
            + ",".join(fields)
            + ") VALUES ("
            + ",".join(":" + field for field in fields)
            + ")"
        ),
        values,
    )


def insert_event(db, release, **changes):
    canonical = b'{"fixture":"INACTIVE-BOOKKEEPING-ONLY"}'
    values = (
        dict(
            id=uuid4(),
            release_id=release["id"],
            purpose="care",
            notice_version="FIXTURE-NOT-APPROVED",
            language="en",
            event_type="publication",
            previous_event_id=None,
            canonical_event=canonical,
            event_digest=sha256(canonical).digest(),
            request_id=uuid4(),
        )
        | changes
    )
    fields = tuple(values)
    db.execute(
        text(
            "INSERT INTO app.consent_publication_events ("
            + ",".join(fields)
            + ") VALUES ("
            + ",".join(":" + field for field in fields)
            + ")"
        ),
        values,
    )
    return values


@pytest.fixture
def seeded(ready):
    with ready[1].begin() as db:
        release = insert_release(db)
        binding = binding_values(release)
        insert_binding(db, binding)
        event = insert_event(db, release)
    return ready, release, binding, event


def test_fresh_install_through_0005_is_empty_inactive_and_unreachable(ready):
    server, admin, runtime = ready
    clinical.check_runtime_role(runtime)
    with admin.connect() as db:
        assert db.scalar(text("SELECT version_num FROM public.alembic_version")) == "0005"
        assert db.scalar(text("SELECT version() LIKE 'PostgreSQL 16.2%'"))
        assert db.scalar(text("SHOW listen_addresses")) == ""
        assert db.scalar(text("SHOW unix_socket_directories")) == str(server.socket)
        assert db.scalar(text("SHOW unix_socket_permissions")) == "0700"
        assert db.scalar(text("SELECT inet_server_addr() IS NULL"))
        assert counts(db) == (0, 0, 0)
        assert db.scalar(text("SELECT count(*) FROM app.consent_notice_versions")) == 0
        assert not db.scalar(
            text("SELECT EXISTS(SELECT FROM app.consent_notice_versions WHERE accepts_new)")
        )
        assert db.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")) == 25
        assert db.scalar(
            text(
                "SELECT bool_and(c.relrowsecurity AND pg_get_userbyid(c.relowner)='app_owner') "
                "FROM pg_class c WHERE c.relnamespace='app'::regnamespace AND c.relkind='r'"
            )
        )
        assert not db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM pg_policy WHERE polrelid IN "
                "(SELECT oid FROM pg_class WHERE relnamespace='app'::regnamespace "
                "AND relname IN ('consent_publication_releases','consent_notice_bindings','consent_publication_events')))"
            )
        )
        attrs = db.execute(
            text(
                "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit "
                "FROM pg_roles WHERE rolname='dhanvantari_notice_publisher'"
            )
        ).one()
        assert attrs == (False,) * 7
        assert not db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM pg_auth_members WHERE member='dhanvantari_notice_publisher'::regrole)"
            )
        )
        assert db.scalar(
            text("SELECT has_schema_privilege('dhanvantari_notice_publisher','app','USAGE')")
        )
        assert not db.scalar(
            text("SELECT has_schema_privilege('dhanvantari_notice_publisher','app','CREATE')")
        )
        assert not db.scalar(
            text(
                "SELECT pg_has_role('dhanvantari_runtime','dhanvantari_notice_publisher','MEMBER')"
            )
        )
        assert not db.scalar(
            text(
                "SELECT EXISTS(SELECT FROM pg_proc WHERE pronamespace='app'::regnamespace "
                "AND proname ~ '^(publish|activate|retire)')"
            )
        )
    assert not server.directory.stat().st_mode & 0o077
    assert not server.socket.stat().st_mode & 0o077


def test_upgrade_from_0004_preserves_functions_policies_defaults_and_existing_history(local_pg):
    migrate(local_pg, "0004")
    admin = local_pg.engine()
    clinical.staff(admin)
    local_pg.create_runtime()
    runtime = local_pg.engine(local_pg.url("dhanvantari_runtime"))
    with admin.begin() as db:
        db.execute(
            text(
                "INSERT INTO app.consent_notice_versions(purpose,notice_version,language,accepts_new) "
                "VALUES('care',:version,'en',true)"
            ),
            {"version": clinical.NOTICE},
        )
    clinical.register((local_pg, admin, runtime))
    with admin.begin() as db:
        db.execute(text("UPDATE app.consent_notice_versions SET accepts_new=false"))
    with admin.connect() as db:
        patients = db.execute(text("SELECT * FROM app.patients ORDER BY id")).all()
        consents = db.execute(text("SELECT * FROM app.consents ORDER BY id")).all()
        audit = db.execute(text("SELECT * FROM app.audit_logs ORDER BY id")).all()
        functions = db.execute(
            text(
                "SELECT oid,proname,prosrc,proacl::text,proconfig::text FROM pg_proc "
                "WHERE pronamespace='app'::regnamespace ORDER BY oid"
            )
        ).all()
        policies = db.execute(
            text(
                "SELECT oid,polname,polroles::text,polqual::text,polwithcheck::text FROM pg_policy "
                "WHERE polrelid IN (SELECT oid FROM pg_class WHERE relnamespace='app'::regnamespace) ORDER BY oid"
            )
        ).all()
        defaults = db.execute(
            text("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid")
        ).all()
        staff = db.execute(text("SELECT * FROM app.staff_profiles ORDER BY id")).all()
    migrate(local_pg)
    with admin.connect() as db:
        assert db.scalar(text("SELECT version_num FROM public.alembic_version")) == "0005"
        assert (
            db.execute(
                text(
                    "SELECT oid,proname,prosrc,proacl::text,proconfig::text FROM pg_proc "
                    "WHERE oid IN :ids ORDER BY oid"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [row.oid for row in functions]},
            ).all()
            == functions
        )
        assert (
            db.execute(
                text(
                    "SELECT oid,polname,polroles::text,polqual::text,polwithcheck::text FROM pg_policy "
                    "WHERE polrelid IN (SELECT oid FROM pg_class WHERE relnamespace='app'::regnamespace) ORDER BY oid"
                )
            ).all()
            == policies
        )
        assert (
            db.execute(text("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid")).all()
            == defaults
        )
        assert db.execute(text("SELECT * FROM app.staff_profiles ORDER BY id")).all() == staff
        assert counts(db) == (0, 0, 0)
        assert db.execute(text("SELECT * FROM app.patients ORDER BY id")).all() == patients
        assert db.execute(text("SELECT * FROM app.consents ORDER BY id")).all() == consents
        assert db.execute(text("SELECT * FROM app.audit_logs ORDER BY id")).all() == audit


def test_effective_privileges_and_unauthorized_writes(ready):
    _, admin, _ = ready
    attempts = 0
    for role in (
        "dhanvantari_runtime",
        "dhanvantari_notice_publisher",
        "anon",
        "authenticated",
        "service_role",
    ):
        with admin.connect() as db:
            for table in TABLES:
                assert not db.scalar(
                    text(
                        "SELECT has_table_privilege(:role,:table,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"role": role, "table": "app." + table},
                )
        for table in TABLES:
            update_field = "content_hash" if table == "consent_notice_bindings" else "request_id"
            for statement in (
                f"INSERT INTO app.{table} DEFAULT VALUES",
                f"UPDATE app.{table} SET {update_field}={update_field}",
                f"DELETE FROM app.{table}",
                f"TRUNCATE app.{table}",
            ):
                with pytest.raises(DBAPIError) as error:
                    with admin.begin() as db:
                        db.execute(text(f"SET LOCAL ROLE {role}"))
                        db.execute(text(statement))
                assert error.value.orig.sqlstate == "42501"
                attempts += 1
    assert attempts == 60


def test_operator_capture_exact_text_and_immutable_rows(seeded):
    ready, release, _, _ = seeded
    admin = ready[1]
    with admin.connect() as db:
        assert db.scalar(
            text(
                "SELECT operator_session_user=session_user AND operator_role_oid=(SELECT oid FROM pg_roles WHERE rolname=session_user) "
                "AND recorded_at>'2000-01-01T00:00:00Z' FROM app.consent_publication_releases"
            )
        )
        assert db.scalar(
            text("SELECT operator_session_user=session_user FROM app.consent_publication_events")
        )
        assert db.scalar(text("SELECT notice_text FROM app.consent_notice_bindings")) == NOTICE_TEXT
        assert (
            db.scalar(text("SELECT canonical_manifest FROM app.consent_publication_releases"))
            == release["canonical_manifest"]
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgtype=34 "
                    "AND tgfoid='app.audit_immutable()'::regprocedure AND tgrelid IN "
                    "(SELECT oid FROM pg_class WHERE relnamespace='app'::regnamespace "
                    "AND relname IN ('consent_publication_releases','consent_notice_bindings','consent_publication_events'))"
                )
            )
            == 3
        )
    for table in TABLES:
        field = "content_hash" if table == "consent_notice_bindings" else "request_id"
        for statement in (f"UPDATE app.{table} SET {field}={field}", f"DELETE FROM app.{table}"):
            with pytest.raises(DBAPIError) as error:
                with admin.begin() as db:
                    db.execute(text(statement))
            assert error.value.orig.sqlstate == "P0001"
    with pytest.raises(DBAPIError) as error:
        with admin.begin() as db:
            db.execute(
                text(
                    "TRUNCATE app.consent_publication_events,app.consent_notice_bindings,app.consent_publication_releases"
                )
            )
    assert error.value.orig.sqlstate == "P0001"
    with admin.connect() as db:
        assert counts(db) == (1, 1, 1)


def test_hash_constraints_and_publication_hard_gate(seeded):
    ready, release, binding, _ = seeded
    admin = ready[1]
    for field in (
        "manifest_digest",
        "evidence_bundle_digest",
        "trust_policy_digest",
        "reviewer_registry_digest",
    ):
        with pytest.raises(DBAPIError) as error:
            with admin.begin() as db:
                insert_release(
                    db, previous_manifest_digest=release["manifest_digest"], **{field: b"\x00" * 32}
                )
        assert error.value.orig.sqlstate == "23514"
    with pytest.raises(DBAPIError) as error:
        with admin.begin() as db:
            insert_release(
                db, previous_manifest_digest=release["manifest_digest"], publication_enabled=True
            )
    assert error.value.orig.sqlstate == "23514"
    for field in ("content_hash", "artifact_digest", "original_approval_evidence_digest"):
        values = binding | {"notice_version": "FICTIONAL-DIFFERENT", field: b"\x00" * 32}
        with pytest.raises(DBAPIError) as error:
            with admin.begin() as db:
                insert_binding(db, values)
        assert error.value.orig.sqlstate == "23514"
    with admin.connect() as db:
        assert not db.scalar(
            text("SELECT bool_or(publication_enabled) FROM app.consent_publication_releases")
        )
        assert counts(db) == (1, 1, 1)


def test_identity_and_chain_constraints_reject_conflicts(seeded):
    ready, release, binding, event = seeded
    admin = ready[1]
    with pytest.raises(DBAPIError) as error:
        with admin.begin() as db:
            insert_release(db)
    assert error.value.orig.sqlstate == "23505"  # second genesis
    with pytest.raises(DBAPIError) as error:
        with admin.begin() as db:
            insert_binding(db, binding)
    assert error.value.orig.sqlstate == "23505"
    with pytest.raises(DBAPIError) as error:
        with admin.begin() as db:
            successor = insert_release(db, previous_manifest_digest=release["manifest_digest"])
            insert_event(db, successor, previous_event_id=uuid4())
    assert error.value.orig.sqlstate == "23503"
    with pytest.raises(DBAPIError) as error:
        with admin.begin() as db:
            insert_event(db, release, event_digest=b"\x00" * 32)
    assert error.value.orig.sqlstate == "23514"
    with admin.connect() as db:
        assert counts(db) == (1, 1, 1)


def test_existing_registration_replay_and_revocation_are_unchanged(ready):
    server, admin, runtime = ready
    clinical.staff(admin)
    # Disposable-only synthetic registry metadata. No real approvals or app enablement.
    with admin.begin() as db:
        db.execute(
            text(
                "INSERT INTO app.consent_notice_versions(purpose,notice_version,language,accepts_new) "
                "VALUES('care',:version,'en',true)"
            ),
            {"version": clinical.NOTICE},
        )
    key = uuid4()
    response = clinical.register(ready, key=key)
    patient = clinical.PatientRegistrationResponse.model_validate(response)
    with admin.begin() as db:
        db.execute(text("UPDATE app.consent_notice_versions SET accepts_new=false"))
    assert clinical.register(ready, key=key) == response
    with pytest.raises(DBAPIError):
        clinical.register(ready)
    with clinical.actor_session(runtime, clinical.NURSE) as db:
        assert db.scalar(text("SELECT app.has_care_consent(:id)"), {"id": patient.id})
    revoked = clinical.revoke(ready, patient.id, patient.consents[0].id)
    assert revoked["care_access"] == "blocked"
    with clinical.actor_session(runtime, clinical.NURSE) as db:
        assert not db.scalar(text("SELECT app.has_care_consent(:id)"), {"id": patient.id})
    assert clinical.register(ready, key=key)["care_access"] == "blocked"
    with admin.connect() as db:
        assert counts(db) == (0, 0, 0)
        assert db.scalar(text("SELECT count(*) FROM app.consents")) == 1
        assert db.scalar(text("SELECT revoked_at IS NOT NULL FROM app.consents"))


def test_mid_migration_failure_rolls_back_new_objects_and_revision(local_pg):
    migrate(local_pg, "0004")
    admin = local_pg.engine()
    # A disposable-only name collision occurs AFTER role/release/binding creation.
    with admin.begin() as db:
        db.execute(text("CREATE TABLE app.consent_publication_events (fictional_tripwire integer)"))
    with pytest.raises(HarnessError) as error:
        local_pg.migrate("0005")
    assert diagnostic(error.value)[0] == "42P07"
    with admin.connect() as db:
        assert db.scalar(text("SELECT version_num FROM public.alembic_version")) == "0004"
        assert not db.scalar(
            text("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname='dhanvantari_notice_publisher')")
        )
        assert db.scalar(text("SELECT to_regclass('app.consent_publication_releases') IS NULL"))
        assert db.scalar(text("SELECT to_regclass('app.consent_notice_bindings') IS NULL"))
        assert db.scalar(
            text("SELECT to_regprocedure('app.capture_consent_publication_operator()') IS NULL")
        )
        assert db.scalar(text("SELECT count(*) FROM app.consent_publication_events")) == 0
