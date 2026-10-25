from uuid import uuid4

import pytest
from sqlalchemy import text

from app.provision import main


def test_operator_provisioning_requires_existing_auth_user(dbs, monkeypatch, capsys):
    admin = dbs[0]
    uid = uuid4()
    # Minimal synthetic auth fixture, never a real Supabase user or credential.
    with admin.begin() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
        c.execute(text("CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY)"))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", admin.url.render_as_string(hide_password=False))
    monkeypatch.setattr(
        "sys.argv",
        [
            "provision",
            "--id",
            str(uid),
            "--role",
            "doctor",
            "--name",
            "Doctor Fictional",
            "--registration",
            "FICTIONAL-100",
        ],
    )
    with pytest.raises(SystemExit):
        main()
    with admin.begin() as c:
        c.execute(text("INSERT INTO auth.users(id) VALUES (:id)"), {"id": uid})
    main()
    with admin.connect() as c:
        assert (
            c.execute(
                text("SELECT role FROM app.staff_profiles WHERE id=:id"), {"id": uid}
            ).scalar_one()
            == "doctor"
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM app.doctors WHERE id=:id"), {"id": uid}
            ).scalar_one()
            == 1
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM app.audit_logs WHERE actor_id=:id"), {"id": uid}
            ).scalar_one()
            == 1
        )
    assert "Clinician provisioned" in capsys.readouterr().out
