from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    environment: str = "development"
    database_url: SecretStr
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
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
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
