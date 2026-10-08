import base64
import json
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from lxml import etree

from app import connections, punchout_config
from app.auth import User
from app.auth.dependencies import require_admin
from app.config import public_base_url
from app.cxml.common import Credential, CXMLError, parse, pretty, redact_secrets, text
from app.cxml.setup import build_setup_request, parse_setup_response
from app.db import get_db
from app.punchout import sessions
from app.punchout.service import handle_setup
from app.routes.admin import render_settings_editor, settings_section_or_404, vendor_or_404
from app.templating import templates

router = APIRouter(prefix="/admin")


# --- connections ------------------------------------------------------------


@router.get("/vendors/{vendor_id}/connections/new", response_class=HTMLResponse)
def connection_new(vendor_id: str, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)):
    vendor = vendor_or_404(conn, vendor_id)
    return templates.TemplateResponse(
        request,
        "admin/connection_new.html",
        {"user": user, "vendor": vendor, "error": None, "form": {"from_domain": "NetworkID"}},
    )


@router.post("/vendors/{vendor_id}/connections", response_class=HTMLResponse)
def connection_create(
    vendor_id: str,
    request: Request,
    name: Annotated[str, Form()],
    from_domain: Annotated[str, Form()],
    from_identity: Annotated[str, Form()],
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    vendor = vendor_or_404(conn, vendor_id)
    try:
        connections.create_connection(conn, vendor.id, name, from_domain, from_identity)
    except connections.InvalidConnection as e:
        form = {"name": name, "from_domain": from_domain, "from_identity": from_identity}
        return templates.TemplateResponse(
            request,
            "admin/connection_new.html",
            {"user": user, "vendor": vendor, "error": str(e), "form": form},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return RedirectResponse(f"/admin/vendors/{vendor_id}", status.HTTP_303_SEE_OTHER)


@router.post("/vendors/{vendor_id}/connections/{connection_pk}/active")
def connection_set_active(
    vendor_id: str,
    connection_pk: int,
    active: Annotated[bool, Form()],
    _: User = Depends(require_admin),
    conn=Depends(get_db),
):
    vendor = vendor_or_404(conn, vendor_id)
    if not connections.set_active(conn, vendor.id, connection_pk, active):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return RedirectResponse(f"/admin/vendors/{vendor_id}", status.HTTP_303_SEE_OTHER)


def connection_or_404(conn, vendor, connection_pk: int) -> connections.Connection:
    connection = connections.get_connection(conn, vendor.id, connection_pk)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return connection


def render_connection_settings(request, user, vendor, connection, section: str, text: str, error=None, **kw):
    return render_settings_editor(
        request,
        user,
        section=section,
        heading=f"{connection.name} · {vendor.name}",
        back_url=f"/admin/vendors/{vendor.vendor_id}",
        back_label=vendor.name,
        settings_url=f"/admin/vendors/{vendor.vendor_id}/connections/{connection.id}/settings",
        text=text,
        layers=[("vendor", vendor.config), ("connection", connection.config)],
        error=error,
        **kw,
    )


@router.get("/vendors/{vendor_id}/connections/{connection_pk}/settings", include_in_schema=False)
def connection_settings_root(vendor_id: str, connection_pk: int, _: User = Depends(require_admin)):
    return RedirectResponse(
        f"/admin/vendors/{vendor_id}/connections/{connection_pk}/settings/general", status.HTTP_303_SEE_OTHER
    )


@router.get("/vendors/{vendor_id}/connections/{connection_pk}/settings/{section}", response_class=HTMLResponse)
def connection_settings(
    vendor_id: str,
    connection_pk: int,
    section: str,
    request: Request,
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    section = settings_section_or_404(section)
    vendor = vendor_or_404(conn, vendor_id)
    connection = connection_or_404(conn, vendor, connection_pk)
    text = punchout_config.to_json(punchout_config.section_overrides(connection.config, section))
    return render_connection_settings(request, user, vendor, connection, section, text)


@router.post("/vendors/{vendor_id}/connections/{connection_pk}/settings/{section}", response_class=HTMLResponse)
def connection_settings_save(
    vendor_id: str,
    connection_pk: int,
    section: str,
    request: Request,
    config: Annotated[str, Form()] = "",
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    section = settings_section_or_404(section)
    vendor = vendor_or_404(conn, vendor_id)
    connection = connection_or_404(conn, vendor, connection_pk)
    try:
        overrides = punchout_config.apply_section(connection.config, section, config, vendor.config)
    except punchout_config.ConfigError as e:
        return render_connection_settings(
            request, user, vendor, connection, section, config, str(e),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    connections.update_config(conn, vendor.id, connection.id, overrides)
    return RedirectResponse(
        f"/admin/vendors/{vendor_id}/connections/{connection_pk}/settings/{section}?saved=1", status.HTTP_303_SEE_OTHER
    )


# --- session history --------------------------------------------------------


@router.get("/vendors/{vendor_id}/sessions/{session_pk}", response_class=HTMLResponse)
def session_detail(
    vendor_id: str, session_pk: int, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)
):
    vendor = vendor_or_404(conn, vendor_id)
    session = sessions.get_session(conn, session_pk)
    if session is None or session.vendor_pk != vendor.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    connection = connections.get_connection(conn, vendor.id, session.connection_pk)
    return templates.TemplateResponse(
        request,
        "admin/session_detail.html",
        {
            "user": user,
            "vendor": vendor,
            "session": session,
            "connection": connection,
            "raw_request": pretty(session.raw_request),
            "lines": sessions.cart_lines(conn, session),
            "fields": json.dumps(session.fields, indent=2, sort_keys=True),
            "order_messages": [
                {**dict(m), "pretty": pretty(m["cxml"])} for m in sessions.order_messages(conn, session.id)
            ],
        },
    )


# --- test tool: act as a procurement system ---------------------------------


def test_form_context(user, vendor, conn, **extra):
    active = [c for c in connections.list_connections(conn, vendor.id) if c.active]
    return {"user": user, "vendor": vendor, "connections": active, "error": None, **extra}


@router.get("/vendors/{vendor_id}/test", response_class=HTMLResponse)
def punchout_test_form(vendor_id: str, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)):
    vendor = vendor_or_404(conn, vendor_id)
    return templates.TemplateResponse(
        request,
        "admin/punchout_test.html",
        test_form_context(user, vendor, conn, form={"user_name": user.display_name, "user_email": "buyer@example.com"}),
    )


@router.post("/vendors/{vendor_id}/test", response_class=HTMLResponse)
def punchout_test_send(
    vendor_id: str,
    request: Request,
    connection_pk: Annotated[int, Form()],
    shared_secret: Annotated[str, Form()],
    user_name: Annotated[str, Form()] = "",
    user_email: Annotated[str, Form()] = "",
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    vendor = vendor_or_404(conn, vendor_id)
    connection = connections.get_connection(conn, vendor.id, connection_pk)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")

    base = public_base_url(request)
    buyer = Credential(connection.from_domain, connection.from_identity)
    posr = build_setup_request(
        from_cred=buyer,
        to_cred=Credential("NetworkID", vendor.vendor_id),
        sender_cred=buyer,
        shared_secret=shared_secret,
        browser_form_post_url=f"{base}/admin/punchout-test/receive",
        buyer_cookie=secrets.token_hex(16),
        extrinsics={"UserEmail": user_email, "UniqueName": user_email, "UserPrintableName": user_name},
    )
    # Call the same code the real endpoint uses, so the test covers secret checks and connection lookup.
    result = handle_setup(conn, vendor.vendor_id, posr, base)
    code, message, start_page = parse_setup_response(result.body)
    if code == 200 and start_page:
        return RedirectResponse(start_page, status.HTTP_303_SEE_OTHER)

    form = {"connection_pk": connection_pk, "user_name": user_name, "user_email": user_email}
    return templates.TemplateResponse(
        request,
        "admin/punchout_test.html",
        test_form_context(
            user, vendor, conn, form=form, error=f"{code} {message}",
            request_xml=pretty(redact_secrets(posr.decode())),
            response_xml=pretty(result.body),
        ),
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


@router.post("/punchout-test/receive", response_class=HTMLResponse)
async def punchout_test_receive(request: Request, user: User = Depends(require_admin)):
    """Stands in for the procurement system's BrowserFormPost URL and shows what was received."""
    form = await request.form()
    if encoded := form.get("cxml-base64"):
        raw = base64.b64decode(str(encoded))
    else:
        raw = str(form.get("cxml-urlencoded", "")).encode()

    lines, error, total = [], None, None
    try:
        root = parse(raw)
        msg = root.find("Message/PunchOutOrderMessage")
        if msg is None:
            raise CXMLError(400, "Bad Request", "Not a PunchOutOrderMessage")
        money = msg.find("PunchOutOrderMessageHeader/Total/Money")
        total = f"{text(money)} {money.get('currency', '')}" if money is not None else None
        for item in msg.findall("ItemIn"):
            price = item.find("ItemDetail/UnitPrice/Money")
            lines.append(
                {
                    "sku": text(item, "ItemID/SupplierPartID"),
                    "description": text(item, "ItemDetail/Description"),
                    "quantity": item.get("quantity", ""),
                    "unit_price": f"{text(price)} {price.get('currency', '')}" if price is not None else "",
                    "uom": text(item, "ItemDetail/UnitOfMeasure"),
                }
            )
    except (CXMLError, etree.XMLSyntaxError) as e:
        error = str(e)

    return templates.TemplateResponse(
        request,
        "admin/punchout_test_received.html",
        {"user": user, "lines": lines, "total": total, "error": error, "raw": pretty(raw) if raw else ""},
    )
