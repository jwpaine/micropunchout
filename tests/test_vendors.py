import re
import sqlite3

import pytest

from app import vendors
from app.db import apply_schema
from tests.conftest import HTML, login


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c)
    yield c
    c.close()


def secret_from(html: str) -> str:
    return re.search(r'<code id="secret">([A-Za-z0-9]+)</code>', html).group(1)


# --- repository -------------------------------------------------------------


def test_create_vendor_generates_id_and_secret(conn):
    vendor, secret = vendors.create_vendor(conn, "  Acme Supply  ")
    assert vendor.name == "Acme Supply"
    assert re.fullmatch(r"v[0-9a-f]{12}", vendor.vendor_id)
    assert re.fullmatch(r"[A-Za-z0-9]{32}", secret)
    assert vendor.secret_last4 == secret[-4:]


def test_secret_is_not_stored_in_plaintext(conn):
    _, secret = vendors.create_vendor(conn, "Acme")
    dump = "\n".join(conn.iterdump())
    assert secret not in dump


def test_each_vendor_gets_unique_id_and_secret(conn):
    a, sa = vendors.create_vendor(conn, "A")
    b, sb = vendors.create_vendor(conn, "B")
    assert a.vendor_id != b.vendor_id
    assert sa != sb


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_create_vendor_rejects_bad_names(conn, name):
    with pytest.raises(vendors.VendorError):
        vendors.create_vendor(conn, name)


def test_create_vendor_rejects_duplicate_name_case_insensitive(conn):
    vendors.create_vendor(conn, "Acme")
    with pytest.raises(vendors.VendorError, match="already exists"):
        vendors.create_vendor(conn, "ACME")


def test_verify_secret(conn):
    vendor, secret = vendors.create_vendor(conn, "Acme")
    assert vendors.verify_secret(conn, vendor.vendor_id, secret) == vendor
    assert vendors.verify_secret(conn, vendor.vendor_id, "wrong") is None
    assert vendors.verify_secret(conn, "vnope", secret) is None


def test_secret_does_not_work_for_another_vendor(conn):
    a, secret_a = vendors.create_vendor(conn, "A")
    b, _ = vendors.create_vendor(conn, "B")
    assert vendors.verify_secret(conn, b.vendor_id, secret_a) is None


def test_rotate_secret_invalidates_old(conn):
    vendor, old = vendors.create_vendor(conn, "Acme")
    new = vendors.rotate_secret(conn, vendor.vendor_id)
    assert new != old
    assert vendors.verify_secret(conn, vendor.vendor_id, old) is None
    assert vendors.verify_secret(conn, vendor.vendor_id, new) is not None
    assert vendors.rotate_secret(conn, "vnope") is None


# --- admin pages ------------------------------------------------------------


def test_admin_requires_login(client):
    r = client.get("/admin/vendors", headers=HTML, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fadmin%2Fvendors"
    assert client.post("/admin/vendors", data={"name": "X"}).status_code == 401


def test_create_vendor_via_admin(client):
    login(client)
    r = client.post("/admin/vendors", data={"name": "Acme Supply"})
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-store"
    secret = secret_from(r.text)
    vendor_id = re.search(r"/cxml/v/(v[0-9a-f]{12})/setup", r.text).group(1)

    listing = client.get("/admin/vendors")
    assert "Acme Supply" in listing.text and vendor_id in listing.text

    # The detail page later shows only the last 4 characters.
    detail = client.get(f"/admin/vendors/{vendor_id}")
    assert detail.status_code == 200
    assert secret not in detail.text
    assert f"••••{secret[-4:]}" in detail.text


def test_create_vendor_duplicate_shows_error(client):
    login(client)
    client.post("/admin/vendors", data={"name": "Acme"})
    r = client.post("/admin/vendors", data={"name": "acme"})
    assert r.status_code == 422
    assert "already exists" in r.text


def test_rotate_secret_via_admin(client):
    login(client)
    r = client.post("/admin/vendors", data={"name": "Acme"})
    old = secret_from(r.text)
    vendor_id = re.search(r"/admin/vendors/(v[0-9a-f]{12})/rotate-secret", r.text).group(1)

    r = client.post(f"/admin/vendors/{vendor_id}/rotate-secret")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert secret_from(r.text) != old


def test_unknown_vendor_404(client):
    login(client)
    assert client.get("/admin/vendors/vnope").status_code == 404
    assert client.post("/admin/vendors/vnope/rotate-secret").status_code == 404
