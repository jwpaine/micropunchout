from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import AuthProvider, PasswordAuthProvider, User, current_user, get_auth_provider
from app.auth.dependencies import login_user, logout_user
from app.templating import templates

router = APIRouter()


def safe_next(next_path: str | None) -> str:
    """Only allow same-site relative paths, to avoid open redirects."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//") or "\\" in next_path:
        return "/"
    return next_path


def password_provider(provider: AuthProvider = Depends(get_auth_provider)) -> PasswordAuthProvider:
    if not isinstance(provider, PasswordAuthProvider):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Password login is not enabled")
    return provider


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    next: str | None = None,
    user: User | None = Depends(current_user),
    _: PasswordAuthProvider = Depends(password_provider),
):
    if user is not None:
        return RedirectResponse(safe_next(next), status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
    provider: PasswordAuthProvider = Depends(password_provider),
):
    user = await provider.authenticate(username, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": safe_next(next), "error": "Invalid username or password.", "username": username},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    login_user(request, user)
    return RedirectResponse(safe_next(next), status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
