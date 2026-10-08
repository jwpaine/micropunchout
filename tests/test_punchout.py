import html
import re
import sqlite3
from urllib.parse import urlparse

import pytest
from lxml import etree

from app.cxml.common import Credential
from app.cxml.setup import build_setup_request, parse_setup_request, parse_setup_response
from app.db import MIGRATIONS, SCHEMA, apply_schema
from app.mapping import map_fields
from app.punchout_config import effective
from tests.conftest import login

BUYER = Credential("NetworkID", "AN01000000001")
RETURN_URL = "https://procurement.example.com/punchout/return?session=xyz"
PRODUCTS_CSV = "sku,name,unit_price,currency,uom,unspsc\r\nA,Widget,1.50,USD,EA,31160000\r\nB,Gadget,2.25,USD,BX,\r\n"


@pytest.fixture
def setup(client):
    """A vendor with a secret, one buyer connection and two products. Returns (vendor_id, secret)."""
    login(client)
    r = client.post("/admin/vendors", data={"name": "Acme Supply"})
    secret = re.search(r'<code id="secret">([A-Za-z0-9]+)</code>', r.text).group(1)
    vendor_id = re.search(r"/cxml/v/(v[0-9a-f]{12})/setup", r.text).group(1)
    client.post(
        f"/admin/vendors/{vendor_id}/connections",
        data={"name": "Buyer Corp", "from_domain": BUYER.domain, "from_identity": BUYER.identity},
    )
    client.post(
        f"/admin/vendors/{vendor_id}/products/import",
        files={"file": ("p.csv", PRODUCTS_CSV.encode(), "text/csv")},
    )
    return vendor_id, secret


def posr(secret, *, from_cred=BUYER, operation="create", items=(), cookie="cookie-123", **kw):
    return build_setup_request(
        from_cred=from_cred,
        to_cred=Credential("NetworkID", "SUPPLIER-1"),
        sender_cred=from_cred,
        shared_secret=secret,
        browser_form_post_url=RETURN_URL,
        buyer_cookie=cookie,
        operation=operation,
        extrinsics={"UserEmail": "jane@buyer.example", "UniqueName": "jsmith", "UserPrintableName": "Jane Smith"},
        items=list(items),
        **kw,
    )


def send_setup(client, vendor_id, body):
    r = client.post(f"/cxml/v/{vendor_id}/setup", content=body, headers={"content-type": "text/xml"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/xml")
    return parse_setup_response(r.content)


def start_shopping(client, vendor_id, secret, **kw) -> str:
    """Run setup and follow the StartPage. Returns the shop base path."""
    code, message, start_page = send_setup(client, vendor_id, posr(secret, **kw))
    assert code == 200, message
    r = client.get(urlparse(start_page).path, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def product_id(client, shop, sku):
    page = client.get(shop).text
    block = page.split(f"SKU {sku}")[1]
    return int(re.search(r'name="product_id" value="(\d+)"', block).group(1))


def db_product_id(tmp_path, vendor_id, sku) -> int:
    with sqlite3.connect(tmp_path / "test.db") as db:
        return db.execute(
            "SELECT p.id FROM products p JOIN vendors v ON v.id = p.vendor_pk WHERE v.vendor_id = ? AND p.sku = ?",
            (vendor_id, sku),
        ).fetchone()[0]


def returned_poom(response) -> etree._Element:
    assert response.status_code == 200
    action = re.search(r'<form id="return-form" method="post" action="([^"]+)"', response.text).group(1)
    assert html.unescape(action) == RETURN_URL
    value = re.search(r'name="cxml-urlencoded" value="([^"]*)"', response.text, re.S).group(1)
    xml = html.unescape(value).encode()
    assert b"<!DOCTYPE cXML" in xml
    return etree.fromstring(xml)


# --- round trip -------------------------------------------------------------


def test_full_round_trip(client, setup):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret)
    assert re.fullmatch(r"/shop/[\w-]{43}", shop)

    catalog = client.get(shop)
    assert catalog.status_code == 200
    assert "Widget" in catalog.text and "Gadget" in catalog.text
    assert "Jane Smith" in catalog.text
    assert catalog.headers["referrer-policy"] == "no-referrer"

    a, b = product_id(client, shop, "A"), product_id(client, shop, "B")
    r = client.post(f"{shop}/cart/add", data={"product_id": a, "quantity": 2}, follow_redirects=False)
    assert r.status_code == 303 and "added=1" in r.headers["location"]
    client.post(f"{shop}/cart/add", data={"product_id": b, "quantity": 1})
    client.post(f"{shop}/cart/add", data={"product_id": a, "quantity": 1})  # adds to existing line

    cart = client.get(f"{shop}/cart")
    assert "Total: 6.75 USD" in cart.text  # 3 × 1.50 + 1 × 2.25

    root = returned_poom(client.post(f"{shop}/checkout"))
    msg = root.find("Message/PunchOutOrderMessage")
    assert msg.findtext("BuyerCookie") == "cookie-123"
    assert msg.find("PunchOutOrderMessageHeader").get("operationAllowed") == "edit"
    money = msg.find("PunchOutOrderMessageHeader/Total/Money")
    assert (money.text, money.get("currency")) == ("6.75", "USD")

    items = msg.findall("ItemIn")
    assert [(i.findtext("ItemID/SupplierPartID"), i.get("quantity")) for i in items] == [("A", "3"), ("B", "1")]
    detail = items[0].find("ItemDetail")
    assert [c.tag for c in detail] == ["UnitPrice", "Description", "UnitOfMeasure", "Classification"]
    assert detail.findtext("UnitPrice/Money") == "1.50"
    assert detail.findtext("Classification") == "31160000"
    assert items[1].findtext("ItemDetail/UnitOfMeasure") == "BX"

    # Credentials are echoed back swapped, and the shared secret never goes to the browser.
    assert root.find("Header/To/Credential/Identity").text == BUYER.identity
    assert root.find("Header/From/Credential/Identity").text == "SUPPLIER-1"
    assert root.find(".//SharedSecret") is None
    assert secret.encode() not in etree.tostring(root)


def test_session_is_closed_after_return(client, setup, tmp_path):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret)
    client.post(f"{shop}/checkout")
    assert client.get(shop).status_code == 410
    assert client.post(f"{shop}/checkout").status_code == 410  # no double submit
    a = db_product_id(tmp_path, vendor_id, "A")
    assert client.post(f"{shop}/cart/add", data={"product_id": a}).status_code == 410


def test_empty_cart_returns_empty_poom(client, setup):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret)
    root = returned_poom(client.post(f"{shop}/checkout"))
    assert root.findall("Message/PunchOutOrderMessage/ItemIn") == []
    assert root.findtext("Message/PunchOutOrderMessage/PunchOutOrderMessageHeader/Total/Money") == "0"


def test_admin_session_history(client, setup):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret)
    client.post(f"{shop}/cart/add", data={"product_id": product_id(client, shop, "A"), "quantity": 1})
    client.post(f"{shop}/checkout")

    vendor_page = client.get(f"/admin/vendors/{vendor_id}").text
    assert "Buyer Corp" in vendor_page and "returned" in vendor_page
    session_pk = re.search(rf"/admin/vendors/{vendor_id}/sessions/(\d+)", vendor_page).group(1)
    detail = client.get(f"/admin/vendors/{vendor_id}/sessions/{session_pk}").text
    assert "Jane Smith" in detail
    assert "PunchOutOrderMessage" in detail and "PunchOutSetupRequest" in detail
    assert "total 1.50 USD" in detail


# --- setup errors -----------------------------------------------------------


def test_wrong_secret_and_unknown_vendor_look_the_same(client, setup):
    vendor_id, _ = setup
    assert send_setup(client, vendor_id, posr("wrong"))[:2] == (401, "Invalid credentials")
    assert send_setup(client, "vdoesnotexist", posr("wrong"))[:2] == (401, "Invalid credentials")


def test_unknown_buyer_is_rejected(client, setup):
    vendor_id, secret = setup
    code, message, _ = send_setup(client, vendor_id, posr(secret, from_cred=Credential("NetworkID", "AN-OTHER")))
    assert code == 403
    assert "NetworkID:AN-OTHER" in message


def test_buyer_credential_match_is_case_insensitive(client, setup):
    vendor_id, secret = setup
    code, _, _ = send_setup(client, vendor_id, posr(secret, from_cred=Credential("networkid", BUYER.identity.lower())))
    assert code == 200


def test_disabled_connection_is_rejected(client, setup):
    vendor_id, secret = setup
    page = client.get(f"/admin/vendors/{vendor_id}").text
    toggle = re.search(rf"/admin/vendors/{vendor_id}/connections/(\d+)/active", page).group(0)
    client.post(toggle, data={"active": "false"})
    assert send_setup(client, vendor_id, posr(secret))[0] == 403


def test_malformed_xml(client, setup):
    vendor_id, _ = setup
    assert send_setup(client, vendor_id, b"<cXML><Header>")[0] == 400
    assert send_setup(client, vendor_id, b"<NotCXML/>")[0] == 400


def test_external_entities_are_not_resolved(client, setup):
    vendor_id, secret = setup
    body = posr(secret, cookie="PLACEHOLDER").replace(
        b'<!DOCTYPE cXML SYSTEM "http://xml.cxml.org/schemas/cXML/1.2.014/cXML.dtd">',
        b'<!DOCTYPE cXML [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
    ).replace(b"PLACEHOLDER", b"&xxe;")
    code, _, start_page = send_setup(client, vendor_id, body)
    if code == 200:
        shop = client.get(urlparse(start_page).path, follow_redirects=False).headers["location"]
        root = returned_poom(client.post(f"{shop}/checkout"))
        assert "root:" not in (root.findtext(".//BuyerCookie") or "")


def test_missing_browser_form_post_is_rejected(client, setup):
    vendor_id, secret = setup
    body = re.sub(rb"<BrowserFormPost>.*?</BrowserFormPost>", b"", posr(secret), flags=re.S)
    assert send_setup(client, vendor_id, body)[0] == 400


# --- shared secret is never stored -------------------------------------------


def test_shared_secret_is_redacted_in_stored_request(client, setup, tmp_path):
    vendor_id, secret = setup
    send_setup(client, vendor_id, posr(secret))

    with sqlite3.connect(tmp_path / "test.db") as db:
        raw = db.execute("SELECT raw_request FROM punchout_sessions").fetchone()[0]
        dump = "\n".join(db.iterdump())
    assert "<SharedSecret>****</SharedSecret>" in raw
    assert "<BuyerCookie>cookie-123</BuyerCookie>" in raw  # the rest is kept as received
    assert secret not in dump

    page = client.get(f"/admin/vendors/{vendor_id}").text
    session_pk = re.search(rf"/admin/vendors/{vendor_id}/sessions/(\d+)", page).group(1)
    detail = client.get(f"/admin/vendors/{vendor_id}/sessions/{session_pk}").text
    assert secret not in detail
    assert "&lt;SharedSecret&gt;****&lt;/SharedSecret&gt;" in detail


def test_migration_redacts_previously_stored_secrets(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        for step in MIGRATIONS[:5]:  # the database as it was before redaction existed
            step(db) if callable(step) else db.execute(step)
        db.execute("PRAGMA user_version = 5")
        db.execute(
            "INSERT INTO vendors (vendor_id, name, secret_hash, secret_last4, created_at, secret_rotated_at)"
            " VALUES ('v1', 'V', 'h', 'abcd', 'now', 'now')"
        )
        db.execute("INSERT INTO connections (vendor_pk, name, from_domain, from_identity, created_at) VALUES (1, 'B', 'd', 'i', 'now')")
        db.execute(
            """INSERT INTO punchout_sessions (vendor_pk, connection_pk, start_token, shop_token, status, operation,
                   buyer_cookie, browser_form_post_url, payload_id, raw_request, from_credential, to_credential,
                   created_at, start_expires_at, expires_at)
               VALUES (1, 1, 's', 't', 'returned', 'create', 'c', 'https://r', 'p',
                   '<Sender><Credential><SharedSecret>leaked-secret</SharedSecret></Credential></Sender>',
                   '[]', '[]', 'now', 'now', 'now')"""
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        apply_schema(db)
        raw = db.execute("SELECT raw_request FROM punchout_sessions").fetchone()[0]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert raw == "<Sender><Credential><SharedSecret>****</SharedSecret></Credential></Sender>"


# --- start tokens -----------------------------------------------------------


def test_start_token_works_once(client, setup):
    vendor_id, secret = setup
    _, _, start_page = send_setup(client, vendor_id, posr(secret))
    path = urlparse(start_page).path
    assert client.get(path, follow_redirects=False).status_code == 303
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 410
    assert "expired or was already used" in r.text


def test_start_token_expires(client, setup, tmp_path):
    vendor_id, secret = setup
    _, _, start_page = send_setup(client, vendor_id, posr(secret))
    db = sqlite3.connect(tmp_path / "test.db")
    db.execute("UPDATE punchout_sessions SET start_expires_at = '2000-01-01T00:00:00+00:00'")
    db.commit()
    db.close()
    assert client.get(urlparse(start_page).path, follow_redirects=False).status_code == 410


def test_unknown_shop_token(client):
    assert client.get("/shop/nope").status_code == 404


# --- edit / inspect ---------------------------------------------------------


def test_edit_restores_cart(client, setup):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret, operation="edit", items=[("A", 3), ("MISSING", 1)])
    cart = client.get(f"{shop}/cart").text
    assert "SKU A" in cart and "Total: 4.50 USD" in cart
    assert "MISSING" not in cart


def test_inspect_is_read_only(client, setup, tmp_path):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret, operation="inspect", items=[("A", 1)])
    assert "view only" in client.get(shop).text
    assert 'action="' + shop + '/cart/add"' not in client.get(shop).text
    a = db_product_id(tmp_path, vendor_id, "A")
    assert client.post(f"{shop}/cart/add", data={"product_id": a}).status_code == 403
    root = returned_poom(client.post(f"{shop}/checkout"))
    assert root.find(".//PunchOutOrderMessageHeader").get("operationAllowed") == "inspect"
    assert len(root.findall(".//ItemIn")) == 1


# --- cart rules -------------------------------------------------------------


def test_cannot_add_other_vendors_products(client, setup, tmp_path):
    vendor_id, secret = setup
    # A second vendor with its own product.
    r = client.post("/admin/vendors", data={"name": "Other"})
    other_id = re.search(r"/cxml/v/(v[0-9a-f]{12})/setup", r.text).group(1)
    client.post(
        f"/admin/vendors/{other_id}/products/import",
        files={"file": ("p.csv", b"sku,name,unit_price\r\nZ,Other thing,1\r\n", "text/csv")},
    )
    other_product = db_product_id(tmp_path, other_id, "Z")

    shop = start_shopping(client, vendor_id, secret)
    client.post(f"{shop}/cart/add", data={"product_id": other_product, "quantity": 1})
    assert "Your cart is empty" in client.get(f"{shop}/cart").text


def test_deactivated_product_blocks_checkout(client, setup):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret)
    client.post(f"{shop}/cart/add", data={"product_id": product_id(client, shop, "B"), "quantity": 1})
    # Full sync without B deactivates it while it's in the cart.
    client.post(
        f"/admin/vendors/{vendor_id}/products/import",
        files={"file": ("p.csv", b"sku,name,unit_price\r\nA,Widget,1.50\r\n", "text/csv")},
        data={"deactivate_missing": "true"},
    )
    r = client.post(f"{shop}/checkout")
    assert r.status_code == 409
    assert "No longer available: B" in r.text
    assert client.get(shop).status_code == 200  # session still open


def test_quantity_zero_removes_line(client, setup):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret)
    a = product_id(client, shop, "A")
    client.post(f"{shop}/cart/add", data={"product_id": a, "quantity": 2})
    client.post(f"{shop}/cart/update", data={"product_id": a, "quantity": 5})
    assert "Total: 7.50 USD" in client.get(f"{shop}/cart").text
    client.post(f"{shop}/cart/update", data={"product_id": a, "quantity": 0})
    assert "Your cart is empty" in client.get(f"{shop}/cart").text


# --- parsing ----------------------------------------------------------------


def test_user_falls_back_to_contact():
    body = build_setup_request(
        from_cred=BUYER, to_cred=BUYER, sender_cred=BUYER, shared_secret="s",
        browser_form_post_url=RETURN_URL, buyer_cookie="c",
    ).replace(
        b"<BrowserFormPost>",
        b'<Contact role="endUser"><Name xml:lang="en">Sam Lee</Name><Email>sam@buyer.example</Email></Contact>'
        b"<BrowserFormPost>",
    )
    fields, missing = map_fields(effective().posr_rules(), parse_setup_request(body).lookup)
    assert not missing
    assert fields == {
        "user.external_id": "sam@buyer.example", "user.email": "sam@buyer.example", "user.name": "Sam Lee",
        "ship_to.id": "",
    }


# --- admin test tool --------------------------------------------------------


def test_admin_test_tool_round_trip(client, setup):
    vendor_id, secret = setup
    form = client.get(f"/admin/vendors/{vendor_id}/test").text
    connection_pk = re.search(r'<option value="(\d+)"', form).group(1)

    r = client.post(
        f"/admin/vendors/{vendor_id}/test",
        data={"connection_pk": connection_pk, "shared_secret": secret, "user_name": "Tess", "user_email": "t@x.io"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    shop = client.get(urlparse(r.headers["location"]).path, follow_redirects=False).headers["location"]
    client.post(f"{shop}/cart/add", data={"product_id": product_id(client, shop, "A"), "quantity": 4})
    returned = client.post(f"{shop}/checkout")
    action = re.search(r'action="([^"]+)"', returned.text.split('id="return-form"')[1]).group(1)
    assert urlparse(action).path == "/admin/punchout-test/receive"

    value = html.unescape(re.search(r'name="cxml-urlencoded" value="([^"]*)"', returned.text, re.S).group(1))
    received = client.post("/admin/punchout-test/receive", data={"cxml-urlencoded": value})
    assert received.status_code == 200
    assert "Cart received" in received.text and "Total: 6.00 USD" in received.text


def test_admin_test_tool_wrong_secret_shows_error_without_secret(client, setup):
    vendor_id, _ = setup
    form = client.get(f"/admin/vendors/{vendor_id}/test").text
    connection_pk = re.search(r'<option value="(\d+)"', form).group(1)
    r = client.post(
        f"/admin/vendors/{vendor_id}/test", data={"connection_pk": connection_pk, "shared_secret": "guess123"}
    )
    assert r.status_code == 422
    assert "401 Invalid credentials" in r.text
    assert "guess123" not in r.text


def test_admin_test_tool_requires_admin(client, setup):
    client.post("/logout")
    assert client.post("/admin/punchout-test/receive", data={"cxml-urlencoded": "<cXML/>"}).status_code == 401
