from uuid import uuid4

import pytest
from app.db.session import actor_session, check_runtime_role
from conftest import DOCTOR, NURSE, OTHER
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def seed_case(admin):
    patient, visit, consent = uuid4(), uuid4(), uuid4()
    with admin.begin() as c:
        c.execute(
            text("""INSERT INTO app.patients
          (id,reference,name,age,age_recorded_on,preferred_language,registered_by)
          VALUES (:p,'FICTIONAL-001','Patient Example',36,current_date,'hi',:n)"""),
            {"p": patient, "n": NURSE},
        )
        for staff in (NURSE, DOCTOR):
            c.execute(
                text("INSERT INTO app.patient_assignments(patient_id,staff_id) VALUES (:p,:s)"),
                {"p": patient, "s": staff},
            )
        c.execute(
            text("""INSERT INTO app.consents
          (id,patient_id,purpose,notice_version,language,granted,recorded_by)
          VALUES (:c,:p,'care','fictional-v1','hi',true,:n)"""),
            {"c": consent, "p": patient, "n": NURSE},
        )
        c.execute(
            text("""INSERT INTO app.visits
          (id,patient_id,nurse_id,doctor_id,visit_date,language)
          VALUES (:v,:p,:n,:d,current_date,'hi')"""),
            {"v": visit, "p": patient, "n": NURSE, "d": DOCTOR},
        )
        c.execute(
            text("""INSERT INTO app.symptoms
          (id,visit_id,original_text,original_language,recorded_by)
          VALUES (:id,:v,'काल्पनिक लक्षण','hi',:n)"""),
            {"id": uuid4(), "v": visit, "n": NURSE},
        )
    return patient, visit, consent


def test_schema_all_tables_rls_and_restricted_owner(dbs):
    with dbs[0].connect() as c:
        rows = c.execute(
            text("""SELECT c.relname,c.relrowsecurity,r.rolname
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='app' AND c.relkind='r'""")
        ).all()
        assert len(rows) == 20
        assert all(rls and owner == "app_owner" for _, rls, owner in rows)
    check_runtime_role(dbs[1])
    with pytest.raises(RuntimeError):
        check_runtime_role(dbs[0])


def test_patient_isolation_original_language_and_no_identity_leak(dbs):
    patient, _, _ = seed_case(dbs[0])
    with actor_session(dbs[1], NURSE) as db:
        assert db.execute(text("SELECT id FROM app.patients")).scalars().all() == [patient]
        assert (
            db.execute(text("SELECT original_text FROM app.symptoms")).scalar_one()
            == "काल्पनिक लक्षण"
        )
    with actor_session(dbs[1], OTHER) as db:
        assert db.execute(text("SELECT count(*) FROM app.patients")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM app.symptoms")).scalar_one() == 0
        assert db.execute(text("SELECT id FROM app.staff_profiles")).scalar_one() == OTHER
    with dbs[1].connect() as c:
        assert c.execute(text("SELECT current_setting('request.jwt.claim.sub',true)")).scalar() in (
            None,
            "",
        )
        assert c.execute(text("SELECT current_user")).scalar_one() == "test_runtime"


def test_consent_withdrawal_assignment_revocation_and_inactive_staff(dbs):
    patient, _, consent = seed_case(dbs[0])
    with dbs[0].begin() as c:
        c.execute(text("UPDATE app.consents SET revoked_at=now() WHERE id=:id"), {"id": consent})
    with actor_session(dbs[1], NURSE) as db:
        assert db.execute(text("SELECT count(*) FROM app.patients")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM app.visits")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM app.symptoms")).scalar_one() == 0
    with dbs[0].begin() as c:
        c.execute(
            text("UPDATE app.patient_assignments SET revoked_at=now() WHERE staff_id=:id"),
            {"id": NURSE},
        )
    with actor_session(dbs[1], NURSE) as db:
        assert db.execute(text("SELECT count(*) FROM app.patients")).scalar_one() == 0
    with dbs[0].begin() as c:
        c.execute(text("UPDATE app.staff_profiles SET active=false WHERE id=:id"), {"id": DOCTOR})
    with actor_session(dbs[1], DOCTOR) as db:
        assert db.execute(text("SELECT count(*) FROM app.patients")).scalar_one() == 0


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.staff_profiles SET role='doctor'",
        "DELETE FROM app.patients",
        "DELETE FROM app.clinician_decisions",
        "DELETE FROM app.prescriptions",
        "SELECT * FROM app.auth_sessions",
        "SELECT * FROM app.uploaded_files",
        "SELECT * FROM app.audit_logs",
        "SET LOCAL ROLE app_owner",
    ],
)
def test_authenticated_cannot_escalate_or_write_clinical_data(dbs, statement):
    with pytest.raises(DBAPIError), actor_session(dbs[1], NURSE) as db:
        db.execute(text(statement))


def test_audit_is_append_only_and_private(dbs):
    with dbs[1].begin() as c:
        c.execute(
            text("""INSERT INTO app.audit_logs(id,action,outcome,request_id)
          VALUES (:i,'test','success',:r)"""),
            {"i": uuid4(), "r": uuid4()},
        )
    for query in [
        "SELECT * FROM app.audit_logs",
        "UPDATE app.audit_logs SET outcome='changed'",
        "DELETE FROM app.audit_logs",
        "TRUNCATE app.audit_logs",
    ]:
        with pytest.raises(DBAPIError), dbs[1].begin() as c:
            c.execute(text(query))
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        c.execute(text("DELETE FROM app.audit_logs"))


def test_profession_mismatch_and_prescription_without_approval_rejected(dbs):
    _, visit, _ = seed_case(dbs[0])
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        c.execute(
            text("INSERT INTO app.doctors(id,registration_number) VALUES (:id,'FICTIONAL-BAD')"),
            {"id": NURSE},
        )
    decision = uuid4()
    with dbs[0].begin() as c:
        c.execute(
            text("""INSERT INTO app.clinician_decisions
          (id,visit_id,doctor_id,rationale,status,version)
          VALUES (:id,:v,:d,'Fictional pending review','pending',1)"""),
            {"id": decision, "v": visit, "d": DOCTOR},
        )
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        c.execute(
            text("INSERT INTO app.prescriptions(id,decision_id,doctor_id) VALUES (:i,:d,:u)"),
            {"i": uuid4(), "d": decision, "u": DOCTOR},
        )


def test_schema_constraints_for_vitals_and_urgent_acknowledgement(dbs):
    _, visit, _ = seed_case(dbs[0])
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        c.execute(
            text("""INSERT INTO app.vital_signs
          (id,visit_id,recorded_by,measured_at,spo2_percent)
          VALUES (:i,:v,:n,now(),150)"""),
            {"i": uuid4(), "v": visit, "n": NURSE},
        )
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        c.execute(
            text("""INSERT INTO app.urgent_escalations
          (id,visit_id,flagged_by,reason,status)
          VALUES (:i,:v,:n,'Fictional urgent flag','acknowledged')"""),
            {"i": uuid4(), "v": visit, "n": NURSE},
        )


def test_migration_reapply_is_noop(postgres):
    from alembic import command

    command.upgrade(postgres[2], "head")
