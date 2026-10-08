from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.auth import build_auth_provider
from app.auth.dependencies import LoginRequired
from app.config import Settings, load_settings
from app.db import init_db
from app.routes import admin, admin_products, admin_punchout, auth, cxml, pages, shop
from app.routes.shop import ShopUnavailable, render_unavailable


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings.database_path)
        yield

    app = FastAPI(title="Punchout", lifespan=lifespan)
    app.state.settings = settings
    app.state.auth_provider = build_auth_provider(settings)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="punchout_session",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    app.add_exception_handler(ShopUnavailable, render_unavailable)

    @app.middleware("http")
    async def shopper_page_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/shop/", "/punchout/")):
            # Shop URLs contain the session token: don't leak it to image hosts or other sites via Referer.
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(LoginRequired)
    async def redirect_to_login(request: Request, exc: LoginRequired):
        query = urlencode({"next": exc.next_path}) if exc.next_path != "/" else ""
        return RedirectResponse(f"/login?{query}" if query else "/login", status_code=303)

    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(admin.router)
    app.include_router(admin_products.router)
    app.include_router(admin_punchout.router)
    app.include_router(cxml.router)
    app.include_router(shop.router)
    return app


app = create_app()
