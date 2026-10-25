import httpx
import pytest
from conftest import NURSE
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.services.supabase_auth import SupabaseAuth


@pytest.mark.parametrize(
    "overrides",
    [
        {"cookie_secure": False, "environment": "production"},
        {"supabase_url": "http://remote.example.com"},
        {"allowed_origin": "https://user:secret@example.com"},
        {"allowed_origin": "https://example.com/path"},
        {"database_url": "sqlite://"},
        {"session_ttl_seconds": 99999},
        {"session_encryption_key": "invalid"},
    ],
)
def test_invalid_security_configuration(settings, overrides):
    data = settings.model_dump()
    with pytest.raises((ValidationError, ValueError)):
        Settings(_env_file=None, **{**data, **overrides})


def test_supabase_password_and_user_contract(settings):
    def handler(request):
        assert request.headers["apikey"] == "fictional-publishable-key"
        if request.url.path.endswith("/token"):
            assert request.url.params["grant_type"] == "password"
            return httpx.Response(200, json={"access_token": "fictional-token", "expires_in": 900})
        assert request.headers["authorization"] == "Bearer fictional-token"
        return httpx.Response(200, json={"id": str(NURSE)})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = SupabaseAuth(settings, client)
        token, ttl = provider.login("nurse@example.com", "fictional-test-password")
        assert ttl == 900 and provider.user(token) == NURSE


@pytest.mark.parametrize("status,expected", [(400, 401), (401, 401), (429, 429), (500, 503)])
def test_supabase_error_redaction(settings, status, expected):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"detail": "secret-provider-detail"})
        )
    ) as client:
        with pytest.raises(HTTPException) as exc:
            SupabaseAuth(settings, client).login("nurse@example.com", "fictional-password")
        assert exc.value.status_code == expected
        assert "secret-provider-detail" not in exc.value.detail


def test_supabase_network_and_malformed_response_fail_closed(settings):
    def offline(request):
        raise httpx.ConnectError("hidden-network-detail")

    for handler in [
        offline,
        lambda r: httpx.Response(200, json={"id": "invalid"}),
        lambda r: httpx.Response(200, text="not-json"),
    ]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HTTPException) as exc:
                SupabaseAuth(settings, client).user("fictional-token")
            assert exc.value.status_code == 503
            assert "hidden-network-detail" not in exc.value.detail
