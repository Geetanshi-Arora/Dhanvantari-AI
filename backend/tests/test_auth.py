from datetime import datetime, timedelta, timezone
from uuid import uuid4

from conftest import DOCTOR, NURSE, sign_in
from fastapi import Depends
from sqlalchemy import text

from app.core.security import require_role


def test_health_and_frontend_auth_shell(client):
    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/ready").status_code == 200
    assert "SYNTHETIC PATIENT DEMO" in client.get("/dashboard.html").text
    assert '<main id="main" hidden>' in client.get("/dashboard.html").text
    assert client.get("/api/v1/auth/me").status_code == 401


def test_session_login_me_csrf_logout(client, dbs):
    response = sign_in(client)
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "nurse"
    cookie = response.headers.get_list("set-cookie")
    assert "HttpOnly" in cookie[0] and "SameSite=strict" in cookie[0]
    assert "fictional-provider-token" not in response.text
    with dbs[0].connect() as c:
        encrypted = c.execute(
            text("SELECT encrypted_access_token FROM app.auth_sessions")
        ).scalar_one()
        assert encrypted != "fictional-provider-token"
        assert client.cookies["dhanvantari_session"] not in encrypted
    assert client.get("/api/v1/auth/me").json()["id"] == str(NURSE)
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    assert client.post("/api/v1/auth/logout").status_code == 403
    response = client.post(
        "/api/v1/auth/logout", headers={"Origin": "http://localhost:8000", "X-CSRF-Token": csrf}
    )
    assert response.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    with dbs[0].connect() as c:
        assert c.execute(text("SELECT count(*) FROM app.auth_sessions")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM app.audit_logs")).scalar_one() >= 2


def test_login_origin_and_role_payload(client):
    assert client.post("/api/v1/auth/login", json={}).status_code == 403
    assert sign_in(client, role="doctor").status_code == 422
    assert sign_in(client, password="wrong").status_code == 401
    assert sign_in(client, password="").status_code == 422
    response = sign_in(client, password={"secret": "do-not-echo"})
    assert response.status_code == 422 and "do-not-echo" not in response.text


def test_inactive_unknown_identity(client, dbs):
    client.auth_provider.identity = uuid4()
    assert sign_in(client).status_code == 403
    client.auth_provider.identity = NURSE
    with dbs[0].begin() as c:
        c.execute(text("UPDATE app.staff_profiles SET active=false WHERE id=:id"), {"id": NURSE})
    assert sign_in(client).status_code == 403


def test_role_guard_uses_database_role(client):
    @client.app.get("/api/v1/test-doctor")
    def doctor_only(principal=Depends(require_role("doctor"))):
        return {"role": principal.role}

    # Test-only route must precede the frontend's catch-all static mount.
    client.app.router.routes.insert(0, client.app.router.routes.pop())

    assert sign_in(client).status_code == 200
    assert client.get("/api/v1/test-doctor").status_code == 403
    client.auth_provider.identity = DOCTOR
    assert sign_in(client).status_code == 200
    assert client.get("/api/v1/test-doctor").status_code == 200


def test_session_revocation_expiry_and_identity_recheck(client, dbs):
    assert sign_in(client).status_code == 200
    client.auth_provider.identity = DOCTOR
    assert client.get("/api/v1/auth/me").status_code == 401
    client.auth_provider.identity = NURSE
    client.auth_provider.failure = 503
    assert client.get("/api/v1/auth/me").status_code == 503
    client.auth_provider.failure = None
    with dbs[0].begin() as c:
        c.execute(
            text("UPDATE app.auth_sessions SET expires_at=:e"),
            {"e": datetime.now(timezone.utc) - timedelta(seconds=1)},
        )
    assert client.get("/api/v1/auth/me").status_code == 401


def test_deactivation_applies_to_existing_session(client, dbs):
    assert sign_in(client).status_code == 200
    with dbs[0].begin() as c:
        c.execute(text("UPDATE app.staff_profiles SET active=false WHERE id=:id"), {"id": NURSE})
    assert client.get("/api/v1/auth/me").status_code == 403


def test_throttle_persists_in_database(client, dbs):
    for _ in range(10):
        assert sign_in(client, password="wrong").status_code == 401
    assert sign_in(client).status_code == 429
    with dbs[0].connect() as c:
        assert c.execute(text("SELECT count(*) FROM app.login_throttles")).scalar_one() == 2


def test_cookie_tampering_and_session_rotation(client):
    assert sign_in(client).status_code == 200
    old = client.cookies["dhanvantari_session"]
    assert sign_in(client).status_code == 200
    assert client.cookies["dhanvantari_session"] != old
    client.cookies.clear()
    client.cookies.set("dhanvantari_session", old, path="/api")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_security_headers_and_large_request(client):
    response = client.get("/api/v1/auth/me")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers.get("access-control-allow-origin") is None
    assert client.post("/api/v1/auth/login", content="x" * 17000).status_code == 413


def test_logout_remains_available_during_provider_outage(client):
    assert sign_in(client).status_code == 200
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    client.auth_provider.failure = 503
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"Origin": "http://localhost:8000", "X-CSRF-Token": csrf}
        ).status_code
        == 204
    )
    assert client.get("/api/v1/auth/me").status_code == 401


def test_chunked_oversized_auth_request(client):
    def chunks():
        for _ in range(20):
            yield b"x" * 1000

    assert client.post("/api/v1/auth/login", content=chunks()).status_code == 413


def test_invalid_encrypted_session_and_database_failure(client, dbs):
    assert sign_in(client).status_code == 200
    with dbs[0].begin() as c:
        c.execute(
            text("UPDATE app.auth_sessions SET encrypted_access_token='fictional-corruption'")
        )
    assert client.get("/api/v1/auth/me").status_code == 401
    from unittest.mock import patch

    from sqlalchemy.exc import OperationalError

    with patch("app.routes.auth.throttle", side_effect=OperationalError("hidden-db", {}, None)):
        response = sign_in(client)
    assert response.status_code == 503 and "hidden-db" not in response.text


def test_no_database_connection_held_during_provider_request(client, dbs, monkeypatch):
    assert sign_in(client).status_code == 200

    def check_user(token):
        assert dbs[1].pool.checkedout() == 0
        return NURSE

    monkeypatch.setattr(client.auth_provider, "user", check_user)
    assert client.get("/api/v1/auth/me").status_code == 200
