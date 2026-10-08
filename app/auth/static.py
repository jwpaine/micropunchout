import hmac

from app.auth.base import PasswordAuthProvider, User


class StaticAuthProvider(PasswordAuthProvider):
    """Users defined in config. For development only."""

    name = "static"

    def __init__(self, users: dict[str, str]):
        self._passwords = dict(users)

    def _user(self, username: str) -> User:
        return User(id=username, username=username, display_name=username, roles=frozenset({"admin"}))

    async def authenticate(self, username: str, password: str) -> User | None:
        expected = self._passwords.get(username)
        # Compare against a dummy value for unknown users so timing doesn't reveal which usernames exist.
        ok = hmac.compare_digest(password.encode(), (expected or "\0").encode())
        if expected is None or not ok:
            return None
        return self._user(username)

    async def get_user(self, user_id: str) -> User | None:
        if user_id not in self._passwords:
            return None
        return self._user(user_id)
