from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    id: str
    username: str
    display_name: str
    roles: frozenset[str] = field(default_factory=frozenset)


class AuthProvider(ABC):
    """Looks up users. Every provider must implement this.

    The session stores only ``User.id``; the provider turns it back into a
    ``User`` on each request, so disabled or deleted users lose access.
    """

    name: str

    @abstractmethod
    async def get_user(self, user_id: str) -> User | None: ...


class PasswordAuthProvider(AuthProvider):
    """A provider that checks a username and password (local DB, LDAP, ...).

    Redirect-based providers (OIDC, SAML) should subclass ``AuthProvider``
    and supply their own login/callback routes instead.
    """

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> User | None: ...
