import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import dotenv_values
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    environment: str = "development"
    database_url: SecretStr | None = None
    runtime_credential_file: str | None = None
    runtime_pooler_host: str | None = None
    runtime_ca_file: str | None = None
    supabase_url: str
    supabase_publishable_key: SecretStr
    session_encryption_key: SecretStr
    allowed_origin: str = "http://localhost:8000"
    cookie_secure: bool = True
    session_ttl_seconds: int = 1800
    login_limit: int = 10
    login_window_seconds: int = 900

    @model_validator(mode="after")
    def validate_security(self):
        Fernet(self.session_encryption_key.get_secret_value().encode())
        if self.runtime_credential_file:
            if self.environment != "development":
                raise ValueError("This DPAPI profile is limited to the development project")
            if self.database_url is not None:
                raise ValueError("Conflicting runtime credential sources")
            if not self.runtime_pooler_host or not re.fullmatch(
                r"[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9-]+)*\.pooler\.supabase\.com",
                self.runtime_pooler_host,
            ):
                raise ValueError("Expected a Supabase session pooler host")
            if not self.runtime_ca_file:
                raise ValueError("Runtime CA file is required")
            if self.supabase_url.rstrip("/") != "https://dgrmddokpylbjgpcqrem.supabase.co":
                raise ValueError("Unexpected runtime project")
        elif self.database_url is None or not self.database_url.get_secret_value().startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("DATABASE_URL must use PostgreSQL with psycopg")
        for value in (self.supabase_url, self.allowed_origin):
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("URLs cannot contain credentials, queries or fragments")
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if parsed.scheme != "https" and not (
                self.environment == "development" and local and parsed.scheme == "http"
            ):
                raise ValueError("HTTPS is required except for local development")
            if not parsed.hostname:
                raise ValueError("URL host is required")
        if urlsplit(self.allowed_origin).path not in ("", "/"):
            raise ValueError("ALLOWED_ORIGIN must be an origin")
        self.allowed_origin = self.allowed_origin.rstrip("/")
        self.supabase_url = self.supabase_url.rstrip("/")
        if self.environment != "development" and not self.cookie_secure:
            raise ValueError("Secure cookies required outside development")
        if not 60 <= self.session_ttl_seconds <= 3600:
            raise ValueError("Session lifetime must be between 60 and 3600 seconds")
        if not 1 <= self.login_limit <= 100 or not 60 <= self.login_window_seconds <= 3600:
            raise ValueError("Invalid login throttle settings")
        return self


RUNTIME_FILE = Path(__file__).resolve().parents[2] / "runtime.env"
RUNTIME_KEYS = {
    "ENVIRONMENT",
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SESSION_ENCRYPTION_KEY",
    "ALLOWED_ORIGIN",
    "COOKIE_SECURE",
    "SESSION_TTL_SECONDS",
    "LOGIN_LIMIT",
    "LOGIN_WINDOW_SECONDS",
    "RUNTIME_CREDENTIAL_FILE",
    "RUNTIME_POOLER_HOST",
    "RUNTIME_CA_FILE",
}


def load_runtime_settings() -> Settings:
    """Never open the administrative .env; interpolation cannot pull secrets into values."""
    if any(
        key in os.environ
        for key in (
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "PGHOSTADDR",
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGOPTIONS",
            "PGPASSWORD",
            "PGPASSFILE",
        )
    ):
        raise ValueError("Remove conflicting database overrides from the server environment")
    if not RUNTIME_FILE.is_file() or RUNTIME_FILE.is_symlink():
        raise ValueError("Missing or invalid dedicated runtime configuration")
    values = dotenv_values(RUNTIME_FILE, interpolate=False)
    if set(values) - RUNTIME_KEYS:
        raise ValueError("Missing or invalid dedicated runtime configuration")
    settings = Settings(_env_file=None, **{key.lower(): value for key, value in values.items()})
    if not settings.runtime_credential_file or settings.database_url is not None:
        raise ValueError("Dedicated runtime credentials are required")
    return settings
