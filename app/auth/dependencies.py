from fastapi import Depends, HTTPException, Request, status

from app.auth.base import AuthProvider, User

SESSION_USER_KEY = "user_id"


def get_auth_provider(request: Request) -> AuthProvider:
    return request.app.state.auth_provider


async def current_user(
    request: Request, provider: AuthProvider = Depends(get_auth_provider)
) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    user = await provider.get_user(user_id)
    if user is None:
        # User no longer exists in the provider; drop the stale session.
        request.session.clear()
    return user


class LoginRequired(Exception):
    """Raised for unauthenticated page requests; handled by a redirect to /login."""

    def __init__(self, next_path: str):
        self.next_path = next_path


async def require_user(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is not None:
        return user
    if "text/html" in request.headers.get("accept", ""):
        raise LoginRequired(request.url.path)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


async def require_admin(user: User = Depends(require_user)) -> User:
    if "admin" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


def login_user(request: Request, user: User) -> None:
    # Start a fresh session so nothing from before login carries over.
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()
