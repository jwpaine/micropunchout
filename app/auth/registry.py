from collections.abc import Callable

from app.auth.base import AuthProvider
from app.auth.static import StaticAuthProvider
from app.config import Settings

# Register new providers here, keyed by the AUTH_PROVIDER setting.
PROVIDERS: dict[str, Callable[[Settings], AuthProvider]] = {
    "static": lambda s: StaticAuthProvider(s.static_users),
}


def build_auth_provider(settings: Settings) -> AuthProvider:
    try:
        factory = PROVIDERS[settings.auth_provider]
    except KeyError:
        raise ValueError(
            f"Unknown AUTH_PROVIDER {settings.auth_provider!r}; expected one of {sorted(PROVIDERS)}"
        ) from None
    return factory(settings)
