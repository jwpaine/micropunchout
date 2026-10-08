import os
import warnings
from dataclasses import dataclass, field

from fastapi import Request

DEV_SECRET_KEY = "dev-insecure-secret-key-change-me"


@dataclass(frozen=True)
class Settings:
    secret_key: str = DEV_SECRET_KEY
    database_path: str = "punchout.db"
    # Public URL of this app, used in StartPage URLs sent to procurement systems.
    # Set it when running behind a proxy; otherwise the request's own URL is used.
    public_base_url: str = ""
    # Which auth provider to load; see app.auth.registry.
    auth_provider: str = "static"
    # Mark the session cookie Secure (HTTPS only). Enable in production.
    session_https_only: bool = False
    session_max_age: int = 8 * 60 * 60  # seconds
    # Credentials for the "static" provider. Development only.
    static_users: dict[str, str] = field(default_factory=lambda: {"admin": "password123"})


def load_settings() -> Settings:
    settings = Settings(
        secret_key=os.environ.get("SECRET_KEY", DEV_SECRET_KEY),
        database_path=os.environ.get("DATABASE_PATH", "punchout.db"),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", ""),
        auth_provider=os.environ.get("AUTH_PROVIDER", "static"),
        session_https_only=os.environ.get("SESSION_HTTPS_ONLY", "").lower() in {"1", "true", "yes"},
    )
    if settings.secret_key == DEV_SECRET_KEY:
        warnings.warn("SECRET_KEY is not set; using an insecure development key.", stacklevel=2)
    return settings


def public_base_url(request: Request) -> str:
    return (request.app.state.settings.public_base_url or str(request.base_url)).rstrip("/")
