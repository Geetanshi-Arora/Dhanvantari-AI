"""Supabase Auth boundary. No service-role key or locally trusted demo identities."""

from uuid import UUID

import httpx
from fastapi import HTTPException


class SupabaseAuth:
    def __init__(self, settings, client: httpx.Client):
        self.url = settings.supabase_url + "/auth/v1"
        self.key = settings.supabase_publishable_key.get_secret_value()
        self.client = client

    def _request(self, method, path, **kwargs):
        headers = {"apikey": self.key, **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, self.url + path, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise HTTPException(503, "Authentication service unavailable") from None
        if response.status_code == 429:
            raise HTTPException(429, "Please retry later")
        if response.status_code >= 500:
            raise HTTPException(503, "Authentication service unavailable")
        if not response.is_success:
            raise HTTPException(401, "Invalid or expired credentials")
        try:
            return response.json()
        except ValueError:
            raise HTTPException(503, "Invalid authentication response") from None

    def login(self, email: str, password: str):
        data = self._request(
            "POST", "/token?grant_type=password", json={"email": email, "password": password}
        )
        try:
            token, seconds = data["access_token"], int(data["expires_in"])
            if not isinstance(token, str) or not token or seconds <= 0:
                raise ValueError
            return token, seconds
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(503, "Invalid authentication response") from None

    def user(self, token: str) -> UUID:
        data = self._request("GET", "/user", headers={"Authorization": f"Bearer {token}"})
        try:
            return UUID(data["id"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(503, "Invalid authentication response") from None
