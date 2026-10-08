import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app import connections, punchout_config, vendors
from app.auth import User
from app.auth.dependencies import require_admin
from app.catalog import products
from app.config import public_base_url
from app.db import get_db
from app.punchout import sessions, shoppers
from app.templating import templates

router = APIRouter(prefix="/admin")

# Pages that display a plaintext secret must never be cached.
NO_STORE = {"Cache-Control": "no-store"}


def punchout_url(request: Request, vendor: vendors.Vendor) -> str:
    base = punchout_config.effective(vendor.config).punchout.base_url or public_base_url(request)
    return f"{base}/cxml/v/{vendor.vendor_id}/setup"


def vendor_or_404(conn: sqlite3.Connection, vendor_id: str) -> vendors.Vendor:
    vendor = vendors.get_vendor(conn, vendor_id)
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
    return vendor


def render_detail(
    request: Request,
    conn: sqlite3.Connection,
    user: User,
    vendor: vendors.Vendor,
    new_secret: str | None = None,
    **kw,
):
    return templates.TemplateResponse(
        request,
        "admin/vendor_detail.html",
        {
            "user": user,
            "vendor": vendor,
            "punchout_url": punchout_url(request, vendor),
            "new_secret": new_secret,
            "product_count": products.count_products(conn, vendor.id),
            "connections": connections.list_connections(conn, vendor.id),
            "sessions": sessions.recent_sessions(conn, vendor.id),
            "settings": punchout_config.explain(("vendor", vendor.config)),
            "setting_sections": punchout_config.SECTIONS,
            "shopper_count": shoppers.count_shoppers(conn, vendor.id),
        },
        headers=NO_STORE if new_secret else None,
        **kw,
    )


@router.get("/vendors", response_class=HTMLResponse)
def vendor_list(request: Request, user: User = Depends(require_admin), conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "admin/vendor_list.html", {"user": user, "vendors": vendors.list_vendors(conn)}
    )


@router.get("/vendors/new", response_class=HTMLResponse)
def vendor_new(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin/vendor_new.html", {"user": user, "error": None, "name": ""})


@router.post("/vendors", response_class=HTMLResponse)
def vendor_create(
    request: Request,
    name: Annotated[str, Form()],
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    try:
        vendor, secret = vendors.create_vendor(conn, name)
    except vendors.VendorError as e:
        return templates.TemplateResponse(
            request,
            "admin/vendor_new.html",
            {"user": user, "error": str(e), "name": name},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    # Render the secret directly instead of redirecting: it must not go in a URL or the session cookie.
    return render_detail(request, conn, user, vendor, new_secret=secret, status_code=status.HTTP_201_CREATED)


@router.get("/vendors/{vendor_id}", response_class=HTMLResponse)
def vendor_detail(vendor_id: str, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)):
    return render_detail(request, conn, user, vendor_or_404(conn, vendor_id))


@router.post("/vendors/{vendor_id}/rotate-secret", response_class=HTMLResponse)
def vendor_rotate_secret(
    vendor_id: str, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)
):
    secret = vendors.rotate_secret(conn, vendor_id)
    if secret is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
    return render_detail(request, conn, user, vendor_or_404(conn, vendor_id), new_secret=secret)


def settings_section_or_404(section: str) -> str:
    if section not in punchout_config.SECTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown settings section")
    return section


def render_settings_editor(
    request: Request,
    user: User,
    *,
    section: str,
    heading: str,
    back_url: str,
    back_label: str,
    settings_url: str,
    text: str,
    layers: list[tuple[str, dict]],
    error: str | None = None,
    **kw,
):
    """One section of the JSON settings editor, shared by vendors and connections.

    ``layers`` are the saved overrides, lowest first, used to show each setting's value and source.
    """
    return templates.TemplateResponse(
        request,
        "admin/settings_editor.html",
        {
            "user": user,
            "section": section,
            "sections": punchout_config.SECTIONS,
            "heading": heading,
            "back_url": back_url,
            "back_label": back_label,
            "settings_url": settings_url,
            "text": text,
            "error": error,
            "settings": [r for r in punchout_config.explain(*layers) if r["section"] == section],
            "defaults": punchout_config.to_json(punchout_config.section_defaults(section)),
        },
        **kw,
    )


def render_vendor_settings(request: Request, user: User, vendor: vendors.Vendor, section: str, text: str, error=None, **kw):
    return render_settings_editor(
        request,
        user,
        section=section,
        heading=vendor.name,
        back_url=f"/admin/vendors/{vendor.vendor_id}",
        back_label=vendor.name,
        settings_url=f"/admin/vendors/{vendor.vendor_id}/settings",
        text=text,
        layers=[("vendor", vendor.config)],
        error=error,
        **kw,
    )


@router.get("/vendors/{vendor_id}/settings", include_in_schema=False)
def vendor_settings_root(vendor_id: str, _: User = Depends(require_admin)):
    return RedirectResponse(f"/admin/vendors/{vendor_id}/settings/general", status.HTTP_303_SEE_OTHER)


@router.get("/vendors/{vendor_id}/settings/{section}", response_class=HTMLResponse)
def vendor_settings(
    vendor_id: str, section: str, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)
):
    section = settings_section_or_404(section)
    vendor = vendor_or_404(conn, vendor_id)
    text = punchout_config.to_json(punchout_config.section_overrides(vendor.config, section))
    return render_vendor_settings(request, user, vendor, section, text)


@router.post("/vendors/{vendor_id}/settings/{section}", response_class=HTMLResponse)
def vendor_settings_save(
    vendor_id: str,
    section: str,
    request: Request,
    config: Annotated[str, Form()] = "",
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    section = settings_section_or_404(section)
    vendor = vendor_or_404(conn, vendor_id)
    try:
        overrides = punchout_config.apply_section(vendor.config, section, config)
        # The vendor's settings must also still be valid under each connection's overrides.
        for connection in connections.list_connections(conn, vendor.id):
            try:
                punchout_config.parse_overrides(punchout_config.to_json(connection.config), overrides)
            except punchout_config.ConfigError as e:
                raise punchout_config.ConfigError(f"Conflicts with connection {connection.name!r}: {e}") from None
    except punchout_config.ConfigError as e:
        return render_vendor_settings(
            request, user, vendor, section, config, str(e), status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    vendors.update_config(conn, vendor_id, overrides)
    return RedirectResponse(f"/admin/vendors/{vendor_id}/settings/{section}?saved=1", status.HTTP_303_SEE_OTHER)


@router.get("/vendors/{vendor_id}/shoppers", response_class=HTMLResponse)
def vendor_shoppers(vendor_id: str, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)):
    vendor = vendor_or_404(conn, vendor_id)
    return templates.TemplateResponse(
        request,
        "admin/shopper_list.html",
        {"user": user, "vendor": vendor, "shoppers": shoppers.list_shoppers(conn, vendor.id)},
    )


@router.get("", include_in_schema=False)
def admin_root(_: User = Depends(require_admin)):
    return RedirectResponse("/admin/vendors", status.HTTP_303_SEE_OTHER)
