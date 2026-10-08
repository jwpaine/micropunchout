"""Shopping pages for vendors whose catalog is hosted here.

The shop token in the URL identifies the punchout session, so the pages work without cookies,
including inside procurement-system iframes where third-party cookies are blocked.
"""

import base64
import sqlite3
from decimal import Decimal
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app import connections, punchout_config, vendors
from app.catalog import products
from app.cxml.order_message import MixedCurrencyError, build_order_message
from app.db import get_db
from app.punchout import sessions
from app.punchout.poom import MappingError, order_lines
from app.punchout.sessions import PunchoutSession
from app.templating import templates

router = APIRouter()


class ShopUnavailable(Exception):
    def __init__(self, message: str, status_code: int = status.HTTP_410_GONE):
        self.message = message
        self.status_code = status_code


def render_unavailable(request: Request, exc: ShopUnavailable):
    return templates.TemplateResponse(
        request, "shop/message.html", {"message": exc.message}, status_code=exc.status_code
    )


def shop_session(shop_token: str, conn=Depends(get_db)) -> PunchoutSession:
    session = sessions.get_shop_session(conn, shop_token)
    if session is None:
        raise ShopUnavailable("This shopping session doesn't exist.", status.HTTP_404_NOT_FOUND)
    if session.status == "returned":
        raise ShopUnavailable(
            "This cart has already been sent to your procurement system. To shop again, start a new punchout from there."
        )
    if session.status != "active" or sessions.is_expired(session):
        raise ShopUnavailable("This shopping session has expired. Please start again from your procurement system.")
    return session


def editable_session(session: PunchoutSession = Depends(shop_session)) -> PunchoutSession:
    if session.read_only:
        raise ShopUnavailable("This cart is view-only.", status.HTTP_403_FORBIDDEN)
    return session


def shop_context(conn: sqlite3.Connection, session: PunchoutSession, **extra) -> dict:
    vendor_name = conn.execute("SELECT name FROM vendors WHERE id = ?", (session.vendor_pk,)).fetchone()["name"]
    return {
        "session": session,
        "vendor_name": vendor_name,
        "base": f"/shop/{session.shop_token}",
        "cart_count": sessions.cart_count(conn, session),
        **extra,
    }


@router.get("/punchout/start/{start_token}")
def punchout_start(start_token: str, request: Request, conn=Depends(get_db)):
    session = sessions.start_session(conn, start_token)
    if session is None:
        return render_unavailable(
            request,
            ShopUnavailable(
                "This punchout link has expired or was already used. Please start again from your procurement system."
            ),
        )
    return RedirectResponse(f"/shop/{session.shop_token}", status.HTTP_303_SEE_OTHER)


@router.get("/shop/{shop_token}", response_class=HTMLResponse)
def catalog(
    request: Request,
    q: str = "",
    page: int = 1,
    added: str = "",
    session: PunchoutSession = Depends(shop_session),
    conn=Depends(get_db),
):
    page = max(page, 1)
    items, total = products.search_products(conn, session.vendor_pk, q, page, active_only=True)
    pages = max(1, -(-total // products.PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "shop/catalog.html",
        shop_context(conn, session, products=items, total=total, q=q, page=page, pages=pages, added=added),
    )


@router.post("/shop/{shop_token}/cart/add")
def cart_add(
    product_id: Annotated[int, Form()],
    quantity: Annotated[int, Form()] = 1,
    q: Annotated[str, Form()] = "",
    page: Annotated[int, Form()] = 1,
    session: PunchoutSession = Depends(editable_session),
    conn=Depends(get_db),
):
    ok = sessions.add_to_cart(conn, session, product_id, quantity)
    query = {k: v for k, v in {"q": q, "page": page if page > 1 else "", "added": "1" if ok else ""}.items() if v}
    return RedirectResponse(
        f"/shop/{session.shop_token}" + (f"?{urlencode(query)}" if query else ""), status.HTTP_303_SEE_OTHER
    )


@router.get("/shop/{shop_token}/cart", response_class=HTMLResponse)
def cart(request: Request, session: PunchoutSession = Depends(shop_session), conn=Depends(get_db)):
    return render_cart(request, conn, session)


def render_cart(request: Request, conn: sqlite3.Connection, session: PunchoutSession, error: str = "", status_code=200):
    lines = sessions.cart_lines(conn, session)
    totals: dict[str, Decimal] = {}
    for line in lines:
        totals[line.currency] = totals.get(line.currency, Decimal(0)) + Decimal(line.unit_price) * line.quantity
    return templates.TemplateResponse(
        request,
        "shop/cart.html",
        shop_context(conn, session, lines=lines, totals={c: format(t, "f") for c, t in totals.items()}, error=error),
        status_code=status_code,
    )


@router.post("/shop/{shop_token}/cart/update")
def cart_update(
    product_id: Annotated[int, Form()],
    quantity: Annotated[int, Form()],
    session: PunchoutSession = Depends(editable_session),
    conn=Depends(get_db),
):
    sessions.set_quantity(conn, session, product_id, quantity)
    return RedirectResponse(f"/shop/{session.shop_token}/cart", status.HTTP_303_SEE_OTHER)


@router.post("/shop/{shop_token}/checkout", response_class=HTMLResponse)
def checkout(request: Request, session: PunchoutSession = Depends(shop_session), conn=Depends(get_db)):
    lines = sessions.cart_lines(conn, session)
    if unavailable := [line.sku for line in lines if not line.active]:
        return render_cart(
            request, conn, session, f"No longer available: {', '.join(unavailable)}. Remove them to continue.", 409
        )
    vendor = vendors.get_vendor_by_pk(conn, session.vendor_pk)
    connection = connections.get_connection(conn, session.vendor_pk, session.connection_pk)
    settings = punchout_config.effective(vendor.config, connection.config if connection else {})
    try:
        poom = build_order_message(
            buyer_cookie=session.buyer_cookie,
            buyer_credentials=session.from_credentials,
            supplier_credentials=session.to_credentials,
            lines=order_lines(lines, settings, session.fields),
            operation_allowed="inspect" if session.read_only else "edit",
        )
    except MixedCurrencyError as e:
        return render_cart(request, conn, session, f"{e}. Remove items so the cart uses one currency.", 409)
    except MappingError as e:
        return render_cart(request, conn, session, f"This cart can't be sent: {e}. Please contact the supplier.", 409)

    cxml = poom.cxml.decode()
    stored = sessions.complete_session(
        conn, session, payload_id=poom.payload_id, cxml=cxml, total=poom.total, currency=poom.currency,
        line_count=len(lines),
    )
    if not stored:
        raise ShopUnavailable("This cart has already been sent to your procurement system.")
    field = settings.return_.form_field
    payload = base64.b64encode(poom.cxml).decode() if field == "cxml-base64" else cxml
    return templates.TemplateResponse(
        request,
        "shop/return.html",
        {"action": session.browser_form_post_url, "field": field, "payload": payload, "target": settings.return_.target},
        headers={"Cache-Control": "no-store"},
    )
