"""Prepared PostgreSQL tests. These execute ONLY with the explicit opt-in fixture.

TEST-ONLY notice metadata below is not legal wording or a production approval.
Every test gets its own cluster; no production data or configuration is loaded.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from dhanvantari_pg_guard import HarnessError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import actor_session, check_runtime_role
from app.schemas.patients import PatientRegistrationResponse

NURSE = UUID("10000000-0000-4000-8000-000000000001")
OTHER = UUID("10000000-0000-4000-8000-000000000002")
DOCTOR = UUID("10000000-0000-4000-8000-000000000003")
INACTIVE = UUID("10000000-0000-4000-8000-000000000004")
NOTICE = "TEST-ONLY-NOT-A-LEGAL-NOTICE"


def staff(admin):
    with admin.begin() as db:
        for identity, role in (
            (NURSE, "nurse"),
            (OTHER, "nurse"),
            (DOCTOR, "doctor"),
            (INACTIVE, "nurse"),
        ):
            db.execute(
                text(
                    "INSERT INTO app.staff_profiles(id,display_name,role,active) "
                    "VALUES(:id,:name,:role,:active)"
                ),
                dict(
                    id=identity, name="Fictional Clinician", role=role, active=identity != INACTIVE
                ),
            )
            table = "doctors" if role == "doctor" else "nurses"
            db.execute(
                text(f"INSERT INTO app.{table}(id,registration_number) VALUES(:id,:reg)"),
                dict(id=identity, reg="FICTIONAL-" + str(identity)),
            )


@pytest.fixture
def ready(local_pg):
    local_pg.migrate("0004")
    local_pg.create_runtime()
    admin = local_pg.engine()
    staff(admin)
    return local_pg, admin, local_pg.engine(local_pg.url("dhanvantari_runtime"))


@pytest.fixture
def test_catalog(ready):
    # Executed only inside an approved fresh disposable cluster, never Supabase.
    with ready[1].begin() as db:
        for purpose in ("care", "uploads", "future_ai"):
            db.execute(
                text(
                    "INSERT INTO app.consent_notice_versions "
                    "(purpose,notice_version,language,accepts_new) VALUES(:p,:v,'en',true)"
                ),
                dict(p=purpose, v=NOTICE),
            )


def decision(purpose="care", granted=True):
    return dict(purpose=purpose, granted=granted, notice_version=NOTICE, language="en")


def payload(care=True):
    return dict(
        name="Adult Fictional", age=36, preferred_language="en", consents=[decision(granted=care)]
    )


def register(ready, data=None, key=None, actor=NURSE):
    with actor_session(ready[2], actor) as db:
        return db.scalar(
            text("SELECT app.register_patient(CAST(:p AS jsonb),:k,:r)"),
            dict(p=json.dumps(payload() if data is None else data), k=key or uuid4(), r=uuid4()),
        )


def counts(admin):
    with admin.connect() as db:
        return tuple(
            db.scalar(text(f"SELECT count(*) FROM app.{table}"))
            for table in (
                "patients",
                "patient_assignments",
                "consents",
                "patient_operation_keys",
                "audit_logs",
            )
        )


def change(ready, patient, expected, grant, key=None):
    with actor_session(ready[2], NURSE) as db:
        return db.scalar(
            text("SELECT app.record_consent_decision(:p,CAST(:d AS jsonb),:e,:k,:r)"),
            dict(
                p=patient,
                d=json.dumps(decision(granted=grant)),
                e=expected,
                k=key or uuid4(),
                r=uuid4(),
            ),
        )


def revoke(ready, patient, consent, key=None):
    with actor_session(ready[2], NURSE) as db:
        return db.scalar(
            text("SELECT app.revoke_consent(:p,:c,'care',:k,:r)"),
            dict(p=patient, c=consent, k=key or uuid4(), r=uuid4()),
        )


def test_fresh_install_private_tables_empty_registry_and_role_boundaries(ready):
    _, admin, runtime = ready
    check_runtime_role(runtime)
    with admin.connect() as db:
        assert db.scalar(text("SELECT version_num FROM public.alembic_version")) == "0004"
        assert db.scalar(text("SELECT count(*) FROM app.consent_notice_versions")) == 0
        assert db.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")) == 22
        assert db.scalar(
            text(
                "SELECT bool_and(relrowsecurity) FROM pg_class c JOIN pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname='app' AND c.relkind='r'"
            )
        )
        for role in ("anon", "authenticated", "service_role", "dhanvantari_runtime"):
            assert not db.scalar(
                text("SELECT has_table_privilege(:r,'public.alembic_version','SELECT')"),
                dict(r=role),
            )
        for name in (
            "register_patient",
            "record_consent_decision",
            "revoke_consent",
            "patient_registration_status",
        ):
            functions = db.execute(
                text(
                    "SELECT oid,prosecdef,proconfig,pg_get_userbyid(proowner) "
                    "FROM pg_proc WHERE pronamespace='app'::regnamespace AND proname=:n"
                ),
                dict(n=name),
            ).all()
            assert len(functions) == 1
            oid, definer, settings, owner = functions[0]
            assert definer and owner == "app_owner" and "search_path=pg_catalog" in settings
            for role in ("anon", "authenticated", "service_role"):
                assert not db.scalar(
                    text("SELECT has_function_privilege(:r,:o,'EXECUTE')"), dict(r=role, o=oid)
                )
    for sql in (
        "SELECT * FROM app.patient_operation_keys",
        "SELECT * FROM app.consent_notice_versions",
        "INSERT INTO app.patients(id) VALUES(gen_random_uuid())",
        "SET LOCAL ROLE app_owner",
        "SELECT app._registration_actor()",
    ):
        with pytest.raises(DBAPIError), actor_session(runtime, NURSE) as db:
            db.execute(text(sql))


def test_upgrade_from_0003_preserves_rls_and_defaults(local_pg):
    local_pg.migrate("0003")
    admin = local_pg.engine()
    # Emulate permissive creator defaults only in this disposable cluster.
    with admin.begin() as db:
        db.execute(
            text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT ALL ON TABLES "
                "TO anon,authenticated,service_role"
            )
        )
        db.execute(
            text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT EXECUTE ON FUNCTIONS "
                "TO anon,authenticated,service_role"
            )
        )
    with admin.connect() as db:
        policies = db.execute(
            text(
                "SELECT polname,polroles::text,pg_get_expr(polqual,polrelid) "
                "FROM pg_policy WHERE polrelid IN (SELECT oid FROM pg_class "
                "WHERE relnamespace='app'::regnamespace) ORDER BY polname"
            )
        ).all()
        defaults = db.execute(
            text("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid")
        ).all()
    local_pg.migrate("0004")
    with admin.connect() as db:
        assert db.scalar(text("SELECT version_num FROM public.alembic_version")) == "0004"
        assert (
            db.execute(
                text(
                    "SELECT polname,polroles::text,pg_get_expr(polqual,polrelid) "
                    "FROM pg_policy WHERE polrelid IN (SELECT oid FROM pg_class "
                    "WHERE relnamespace='app'::regnamespace) ORDER BY polname"
                )
            ).all()
            == policies
        )
        assert (
            db.execute(text("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid")).all()
            == defaults
        )
        for role in ("anon", "authenticated", "service_role"):
            for table in ("consent_notice_versions", "patient_operation_keys"):
                assert not db.scalar(
                    text("SELECT has_table_privilege(:r,:t,'SELECT')"),
                    dict(r=role, t="app." + table),
                )
            assert not db.scalar(
                text("SELECT has_function_privilege(:r,'app._registration_actor()','EXECUTE')"),
                dict(r=role),
            )


def test_registration_assignment_optional_absence_and_private_audit(ready, test_catalog):
    result = register(ready)
    PatientRegistrationResponse.model_validate(result)
    assert result["care_access"] == "enabled"
    with ready[1].connect() as db:
        row = db.execute(
            text("SELECT registered_by,fictional,age,reference FROM app.patients")
        ).one()
        assert row.registered_by == NURSE and row.fictional and row.age == 36
        assert len(row.reference) <= 30
        assert db.execute(
            text("SELECT staff_id,revoked_at FROM app.patient_assignments")
        ).one() == (NURSE, None)
        assert db.execute(text("SELECT purpose,granted FROM app.consents")).all() == [
            ("care", True)
        ]
        actions = db.execute(text("SELECT action,outcome FROM app.audit_logs")).all()
        assert set(actions) == {("patient.register", "success"), ("consent.record.care", "success")}
    for actor, visible in ((NURSE, 1), (OTHER, 0), (DOCTOR, 0)):
        with actor_session(ready[2], actor) as db:
            assert db.scalar(text("SELECT count(*) FROM app.patients")) == visible


def test_declined_care_stays_hidden_but_minimal_status_is_available(ready, test_catalog):
    result = register(ready, payload(False))
    assert result["care_access"] == "blocked"
    with actor_session(ready[2], NURSE) as db:
        assert db.scalar(text("SELECT count(*) FROM app.patients")) == 0
        status = db.scalar(
            text("SELECT app.patient_registration_status(:p,:r)"), dict(p=result["id"], r=uuid4())
        )
        assert set(status) == {
            "id",
            "reference",
            "preferred_language",
            "created_at",
            "care_access",
            "consents",
        }
        assert status["consents"][0]["status"] == "declined"


@pytest.mark.parametrize("actor", [DOCTOR, INACTIVE, UUID("10000000-0000-4000-8000-000000000099")])
def test_wrong_or_inactive_actor_cannot_register(ready, test_catalog, actor):
    before = counts(ready[1])
    with pytest.raises(DBAPIError, match="registration.forbidden"):
        register(ready, actor=actor)
    assert counts(ready[1]) == before


@pytest.mark.parametrize(
    "updates",
    [
        {"registered_by": str(OTHER)},
        {"fictional": False},
        {"age": 17},
        {"consents": [decision(), decision()]},
        {"consents": [decision(granted=False)], "history": "Fictional clinical text"},
    ],
)
def test_invalid_fields_and_spoofed_identity_roll_back(ready, test_catalog, updates):
    before = counts(ready[1])
    with pytest.raises(DBAPIError):
        register(ready, {**payload(), **updates})
    assert counts(ready[1]) == before


def test_empty_registry_rejects_registration_without_partial_writes(ready):
    before = counts(ready[1])
    with pytest.raises(DBAPIError, match="registration.notice_not_approved"):
        register(ready)
    assert counts(ready[1]) == before


def test_registration_retry_and_changed_payload_conflict(ready, test_catalog):
    key = uuid4()
    first = register(ready, key=key)
    before = counts(ready[1])
    assert register(ready, key=key)["id"] == first["id"]
    assert counts(ready[1]) == before
    with pytest.raises(DBAPIError, match="registration.idempotency_conflict"):
        register(ready, {**payload(), "age": 37}, key)
    assert counts(ready[1]) == before


def test_concurrent_registration_same_key_creates_one_patient(ready, test_catalog):
    gate, key = Barrier(2), uuid4()

    def attempt():
        gate.wait(timeout=10)
        return register(ready, key=key)["id"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(attempt) for _ in range(2)]
        identities = [future.result(timeout=20) for future in futures]
    assert identities[0] == identities[1]
    assert counts(ready[1]) == (1, 1, 1, 1, 2)


def test_concurrent_consent_updates_have_one_winner(ready, test_catalog):
    patient = register(ready)
    gate = Barrier(2)

    def attempt(granted):
        gate.wait(timeout=10)
        try:
            change(ready, patient["id"], patient["consents"][0]["id"], granted)
            return "success"
        except DBAPIError as error:
            assert "registration.consent_conflict" in str(error.orig)
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(attempt, granted) for granted in (False, True)]
        assert sorted(f.result(timeout=20) for f in results) == ["conflict", "success"]
    with ready[1].connect() as db:
        assert db.scalar(text("SELECT count(*) FROM app.consents")) == 2
        assert db.scalar(text("SELECT count(*) FROM app.consents WHERE revoked_at IS NULL")) == 1


def test_revocation_without_active_notice_and_preserved_history(ready, test_catalog):
    patient = register(ready)
    original = patient["consents"][0]
    with ready[1].begin() as db:
        db.execute(text("UPDATE app.consent_notice_versions SET accepts_new=false"))
    result = revoke(ready, patient["id"], original["id"])
    assert result["care_access"] == "blocked" and result["consent"]["status"] == "revoked"
    assert result["consent"]["granted"] is True
    with actor_session(ready[2], NURSE) as db:
        assert db.scalar(text("SELECT count(*) FROM app.patients")) == 0
    for sql in ("DELETE FROM app.consents", "UPDATE app.consents SET granted=false"):
        with (
            pytest.raises(DBAPIError, match="registration.history_immutable"),
            ready[1].begin() as db,
        ):
            db.execute(text(sql))
    with ready[1].begin() as db:
        db.execute(text("UPDATE app.consent_notice_versions SET accepts_new=true"))
    new = change(ready, patient["id"], original["id"], True)
    assert new["consent"]["id"] != original["id"] and new["care_access"] == "enabled"
    with ready[1].connect() as db:
        assert db.scalar(text("SELECT count(*) FROM app.consents")) == 2
        assert (
            db.scalar(
                text("SELECT granted FROM app.consents WHERE id=:id"), dict(id=original["id"])
            )
            is True
        )


def test_caller_rollback_removes_registration_and_audit(ready, test_catalog):
    before = counts(ready[1])
    with pytest.raises(RuntimeError, match="fictional rollback"):
        with actor_session(ready[2], NURSE) as db:
            db.scalar(
                text("SELECT app.register_patient(CAST(:p AS jsonb),:k,:r)"),
                dict(p=json.dumps(payload()), k=uuid4(), r=uuid4()),
            )
            raise RuntimeError("fictional rollback")
    assert counts(ready[1]) == before


def test_audit_failure_rolls_back_every_registration_write(ready, test_catalog):
    with ready[1].begin() as db:
        db.execute(
            text(
                "CREATE FUNCTION app.fictional_audit_failure() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'FICTIONAL_PRIVATE_AUDIT_MARKER'; END $$"
            )
        )
        db.execute(
            text(
                "CREATE TRIGGER fictional_audit_failure BEFORE INSERT ON app.audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION app.fictional_audit_failure()"
            )
        )
    before = counts(ready[1])
    with pytest.raises(DBAPIError, match="registration.operation_failed") as error:
        register(ready)
    assert "FICTIONAL_PRIVATE_AUDIT_MARKER" not in str(error.value.orig)
    assert counts(ready[1]) == before


def test_unauthorized_status_does_not_wait_for_patient_lock(ready, test_catalog):
    patient = register(ready)
    with ready[1].begin() as blocker:
        blocker.execute(
            text("SELECT id FROM app.patients WHERE id=:p FOR UPDATE"), dict(p=patient["id"])
        )
        with pytest.raises(DBAPIError, match="registration.not_found"):
            with actor_session(ready[2], OTHER) as db:
                db.execute(text("SET LOCAL lock_timeout='500ms'"))
                db.scalar(
                    text("SELECT app.patient_registration_status(:p,:r)"),
                    dict(p=patient["id"], r=uuid4()),
                )


def test_identity_and_role_reset_after_commit_and_rollback(ready, test_catalog):
    register(ready)
    with pytest.raises(RuntimeError), actor_session(ready[2], NURSE):
        raise RuntimeError("fictional rollback")
    with ready[2].connect() as db:
        assert db.scalar(text("SELECT current_user")) == "dhanvantari_runtime"
        assert db.scalar(text("SELECT current_setting('request.jwt.claim.sub',true)")) in (None, "")


def legacy_patient(admin, ambiguous=False):
    staff(admin)
    patient, active = uuid4(), uuid4()
    with admin.begin() as db:
        db.execute(
            text(
                "INSERT INTO app.patients(id,reference,name,age,age_recorded_on,preferred_language,registered_by) "
                "VALUES(:p,'FICTIONAL-LEGACY','Adult Fictional',36,current_date,'en',:n)"
            ),
            dict(p=patient, n=NURSE),
        )
        db.execute(
            text("INSERT INTO app.patient_assignments(patient_id,staff_id) VALUES(:p,:n)"),
            dict(p=patient, n=NURSE),
        )
        for identity, closed in ((uuid4(), True), (active, ambiguous)):
            db.execute(
                text(
                    "INSERT INTO app.consents(id,patient_id,purpose,notice_version,language,granted,recorded_by,created_at,revoked_at) "
                    "VALUES(:id,:p,'care','TEST-LEGACY','en',true,:n,'2026-01-01T00:00:00Z', "
                    "CASE WHEN :closed THEN '2026-01-02T00:00:00Z'::timestamptz ELSE NULL END)"
                ),
                dict(id=identity, p=patient, n=NURSE, closed=closed),
            )
    return patient, active


def test_legacy_tied_creation_times_keep_active_grant_and_allow_revocation(local_pg):
    local_pg.migrate("0003")
    admin = local_pg.engine()
    patient, active = legacy_patient(admin)
    local_pg.migrate("0004")
    local_pg.create_runtime()
    ready = local_pg, admin, local_pg.engine(local_pg.url("dhanvantari_runtime"))
    with actor_session(ready[2], NURSE) as db:
        result = db.scalar(
            text("SELECT app.patient_registration_status(:p,:r)"), dict(p=patient, r=uuid4())
        )
        assert result["care_access"] == "enabled"
        assert result["consents"][0]["id"] == str(active)
    assert revoke(ready, patient, active)["care_access"] == "blocked"


def test_ambiguous_legacy_history_aborts_upgrade_atomically(local_pg):
    local_pg.migrate("0003")
    admin = local_pg.engine()
    legacy_patient(admin, ambiguous=True)
    with pytest.raises(HarnessError, match="Disposable migration failed"):
        local_pg.migrate("0004")
    with admin.connect() as db:
        assert db.scalar(text("SELECT version_num FROM public.alembic_version")) == "0003"
        assert db.scalar(text("SELECT to_regclass('app.patient_operation_keys')")) is None
        assert db.scalar(text("SELECT count(*) FROM app.consents")) == 2


def test_missing_actor_claim_cannot_register(ready, test_catalog):
    before = counts(ready[1])
    with pytest.raises(DBAPIError, match="registration.forbidden"), ready[2].begin() as db:
        db.scalar(
            text("SELECT app.register_patient(CAST(:p AS jsonb),:k,:r)"),
            dict(p=json.dumps(payload()), k=uuid4(), r=uuid4()),
        )
    assert counts(ready[1]) == before


def test_optional_consents_store_only_explicit_choices(ready, test_catalog):
    data = payload()
    data["consents"] += [decision("uploads", False), decision("future_ai", True)]
    register(ready, data)
    with ready[1].connect() as db:
        assert dict(db.execute(text("SELECT purpose,granted FROM app.consents")).all()) == {
            "care": True,
            "uploads": False,
            "future_ai": True,
        }
