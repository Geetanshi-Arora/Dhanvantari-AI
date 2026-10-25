import traceback
from unittest.mock import patch
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.db.session import actor_session, check_runtime_role
from app.main import create_app
from conftest import DOCTOR, NURSE, sign_in
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_database import seed_case


def test_sensitive_patient_data_requires_current_consent(dbs):
    patient, _, consent = seed_case(dbs[0])
    with dbs[0].begin() as c:
        c.execute(
            text(
                "UPDATE app.patients SET history='Fictional history', "
                "allergies='Fictional allergy', current_medicines='Fictional medicine' WHERE id=:p"
            ),
            {"p": patient},
        )
    with actor_session(dbs[1], NURSE) as db:
        assert (
            db.execute(text("SELECT history FROM app.patients")).scalar_one() == "Fictional history"
        )
    for operation in ["UPDATE app.consents SET granted=false", "DELETE FROM app.consents"]:
        with dbs[0].begin() as c:
            c.execute(text(operation))
        with actor_session(dbs[1], NURSE) as db:
            assert (
                db.execute(
                    text("SELECT history,allergies,current_medicines FROM app.patients")
                ).all()
                == []
            )


def test_configuration_and_startup_tracebacks_redact_secrets(settings, dbs):
    marker = "FICTIONAL-SECRET-MUST-NOT-APPEAR"
    values = settings.model_dump()
    values["session_encryption_key"] = marker
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **values)
    assert marker not in "".join(traceback.format_exception(exc.value))
    with patch("app.main.Settings", side_effect=ValueError(marker)):
        with pytest.raises(RuntimeError) as exc:
            create_app()
    assert marker not in "".join(traceback.format_exception(exc.value))
    app = create_app(settings, engine=dbs[1])
    with patch("app.main.check_runtime_role", side_effect=RuntimeError(marker)):
        with pytest.raises(RuntimeError) as exc:
            with TestClient(app):
                pass
    assert marker not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize("privilege", ["CREATEROLE", "CREATEDB", "REPLICATION"])
def test_reject_administrative_runtime_role(dbs, privilege):
    with dbs[0].begin() as c:
        c.execute(text(f"ALTER ROLE dhanvantari_runtime {privilege}"))
    try:
        with pytest.raises(RuntimeError):
            check_runtime_role(dbs[1])
    finally:
        with dbs[0].begin() as c:
            c.execute(text(f"ALTER ROLE dhanvantari_runtime NO{privilege}"))
    check_runtime_role(dbs[1])


def test_reject_membership_in_administrative_role(dbs):
    with dbs[0].begin() as c:
        c.execute(text("CREATE ROLE fictional_admin NOLOGIN CREATEDB"))
        c.execute(text("GRANT fictional_admin TO dhanvantari_runtime"))
    try:
        with pytest.raises(RuntimeError):
            check_runtime_role(dbs[1])
    finally:
        with dbs[0].begin() as c:
            c.execute(text("REVOKE fictional_admin FROM dhanvantari_runtime"))
            c.execute(text("DROP ROLE fictional_admin"))


def test_rejected_requests_are_audited_without_request_secrets(client, dbs):
    assert sign_in(client).status_code == 200
    marker = "FICTIONAL-UNTRUSTED-SECRET"
    assert client.post("/api/v1/auth/logout", headers={"Origin": marker}).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://localhost:8000", "X-CSRF-Token": marker},
        ).status_code
        == 403
    )
    client.cookies.clear()
    client.cookies.set("dhanvantari_session", marker, path="/api")
    assert client.get("/api/v1/auth/me").status_code == 401
    with dbs[0].connect() as c:
        events = c.execute(
            text(
                "SELECT action,outcome,actor_id,resource_id FROM app.audit_logs "
                "WHERE action='security.denied'"
            )
        ).all()
    assert {r.outcome for r in events} == {"origin", "csrf", "session"}
    assert all(r.actor_id is None and r.resource_id is None for r in events)
    assert marker not in str(events)


@pytest.mark.parametrize(
    "invalid",
    [
        "other_visit",
        "missing_assessment",
        "inactive",
        "unassigned",
        "revoked_assignment",
        "no_consent",
        "nurse_actor",
    ],
)
def test_decision_integrity(dbs, invalid):
    patient, visit, _ = seed_case(dbs[0])
    assessment = uuid4()
    with dbs[0].begin() as c:
        target = visit
        if invalid == "other_visit":
            target = uuid4()
            c.execute(
                text(
                    "INSERT INTO app.visits(id,patient_id,nurse_id,visit_date,language) "
                    "VALUES(:v,:p,:n,current_date,'hi')"
                ),
                {"v": target, "p": patient, "n": NURSE},
            )
        if invalid != "missing_assessment":
            c.execute(
                text(
                    "INSERT INTO app.ai_assessments(id,visit_id,provider,model_version,suggestions) "
                    "VALUES(:a,:v,'fictional','fixture','{}')"
                ),
                {"a": assessment, "v": target},
            )
        if invalid == "inactive":
            c.execute(text("UPDATE app.staff_profiles SET active=false WHERE id=:d"), {"d": DOCTOR})
        if invalid == "unassigned":
            c.execute(text("UPDATE app.visits SET doctor_id=NULL WHERE id=:v"), {"v": visit})
        if invalid == "revoked_assignment":
            c.execute(
                text("UPDATE app.patient_assignments SET revoked_at=now() WHERE staff_id=:d"),
                {"d": DOCTOR},
            )
        if invalid == "no_consent":
            c.execute(text("UPDATE app.consents SET revoked_at=now()"))
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        if invalid == "nurse_actor":
            c.execute(
                text("SELECT set_config('request.jwt.claim.sub',:id,true)"), {"id": str(NURSE)}
            )
        c.execute(
            text(
                "INSERT INTO app.clinician_decisions(id,visit_id,doctor_id,assessment_id,"
                "rationale,status,version) VALUES(:id,:v,:d,:a,'Fictional review','approved',1)"
            ),
            {"id": uuid4(), "v": visit, "d": DOCTOR, "a": assessment},
        )


def test_valid_decision_and_update_guard(dbs):
    _, visit, _ = seed_case(dbs[0])
    decision = uuid4()
    with dbs[0].begin() as c:
        c.execute(text("SELECT set_config('request.jwt.claim.sub',:id,true)"), {"id": str(DOCTOR)})
        c.execute(
            text(
                "INSERT INTO app.clinician_decisions(id,visit_id,doctor_id,rationale,status,version) "
                "VALUES(:id,:v,:d,'Fictional review','pending',1)"
            ),
            {"id": decision, "v": visit, "d": DOCTOR},
        )
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        c.execute(
            text("UPDATE app.clinician_decisions SET assessment_id=:a WHERE id=:id"),
            {"a": uuid4(), "id": decision},
        )


def test_matching_assessment_and_immutable_visit(dbs):
    _, visit, _ = seed_case(dbs[0])
    assessment = uuid4()
    with dbs[0].begin() as c:
        c.execute(
            text(
                "INSERT INTO app.ai_assessments(id,visit_id,provider,model_version,suggestions) "
                "VALUES(:a,:v,'fictional','fixture','{}')"
            ),
            {"a": assessment, "v": visit},
        )
        c.execute(
            text(
                "INSERT INTO app.clinician_decisions(id,visit_id,doctor_id,assessment_id,"
                "rationale,status,version) VALUES(:id,:v,:d,:a,'Fictional','pending',1)"
            ),
            {"id": uuid4(), "v": visit, "d": DOCTOR, "a": assessment},
        )
    with pytest.raises(DBAPIError), dbs[0].begin() as c:
        c.execute(
            text("UPDATE app.ai_assessments SET visit_id=:v WHERE id=:a"),
            {"v": uuid4(), "a": assessment},
        )


def test_cli_startup_errors_do_not_expose_configuration(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    marker = "FICTIONAL-CONNECTION-SECRET"
    env = {
        **os.environ,
        "MIGRATION_DATABASE_URL": f"postgresql+psycopg://fictional:{marker}@localhost:not-a-port/postgres",
    }
    commands = [
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(root / "database/alembic.ini"),
            "upgrade",
            "head",
        ],
        [
            sys.executable,
            "-m",
            "app.provision",
            "--id",
            str(uuid4()),
            "--role",
            "nurse",
            "--name",
            "Fictional Nurse",
            "--registration",
            "FICTIONAL-TEST",
        ],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode != 0
        assert marker not in result.stdout + result.stderr
        assert "failed" in result.stderr.lower()


def test_reject_predefined_privileged_role_membership(dbs):
    with dbs[0].begin() as c:
        c.execute(text("GRANT pg_read_all_data TO dhanvantari_runtime"))
    try:
        with pytest.raises(RuntimeError):
            check_runtime_role(dbs[1])
    finally:
        with dbs[0].begin() as c:
            c.execute(text("REVOKE pg_read_all_data FROM dhanvantari_runtime"))
