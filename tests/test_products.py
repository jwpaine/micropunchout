import re
import sqlite3

import pytest

from app import vendors
from app.catalog import products, sources
from app.catalog.products import ProductError, ProductInput
from app.db import apply_schema
from tests.conftest import login

HEADER = "sku,name,unit_price,currency,uom\r\n"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    yield c
    c.close()


@pytest.fixture
def vendor(conn):
    v, _ = vendors.create_vendor(conn, "Acme")
    return v


def item(sku, name="Widget", price="1.00", **kw):
    return ProductInput.from_mapping({"sku": sku, "name": name, "unit_price": price, **kw})


# --- validation -------------------------------------------------------------


def test_from_mapping_normalizes():
    p = item(" W1 ", currency="usd", uom="ea", unit_price="12.50", lead_time_days="3", active="No")
    assert (p.sku, p.currency, p.uom, p.unit_price, p.lead_time_days, p.active) == ("W1", "USD", "EA", "12.50", 3, False)


def test_from_mapping_defaults():
    p = item("W1")
    assert (p.currency, p.uom, p.active, p.lead_time_days) == ("USD", "EA", True, None)


def test_price_uses_plain_notation():
    assert item("W1", unit_price="1E+2").unit_price == "100"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"sku": ""}, "sku is required"),
        ({"name": ""}, "name is required"),
        ({"unit_price": "abc"}, "unit_price must be"),
        ({"unit_price": "-1"}, "unit_price must be"),
        ({"unit_price": "NaN"}, "unit_price must be"),
        ({"unit_price": "1e20"}, "unit_price must be"),
        ({"unit_price": "1.23456"}, "more than 4 decimal places"),
        ({"currency": "US"}, "currency"),
        ({"uom": "EACH"}, "uom"),
        ({"unspsc": "123"}, "unspsc"),
        ({"image_url": "javascript:alert(1)"}, "image_url"),
        ({"lead_time_days": "soon"}, "lead_time_days"),
        ({"active": "maybe"}, "active"),
    ],
)
def test_from_mapping_rejects(overrides, message):
    raw = {"sku": "W1", "name": "Widget", "unit_price": "1", **overrides}
    with pytest.raises(ProductError, match=message):
        ProductInput.from_mapping(raw)


def test_from_mapping_reports_all_problems():
    with pytest.raises(ProductError) as e:
        ProductInput.from_mapping({"sku": "", "name": "", "unit_price": "x"})
    assert str(e.value).count(";") == 2


# --- sync -------------------------------------------------------------------


def test_sync_creates_updates_and_counts_unchanged(conn, vendor):
    r = products.sync_products(conn, vendor.id, [item("A"), item("B")])
    assert (r.created, r.updated, r.unchanged) == (2, 0, 0)

    r = products.sync_products(conn, vendor.id, [item("A"), item("B", price="2.00"), item("C")])
    assert (r.created, r.updated, r.unchanged) == (1, 1, 1)
    found, _ = products.search_products(conn, vendor.id, "B")
    assert found[0].unit_price == "2.00"


def test_sync_deactivate_missing(conn, vendor):
    products.sync_products(conn, vendor.id, [item("A"), item("B")])
    r = products.sync_products(conn, vendor.id, [item("A")], deactivate_missing=True)
    assert r.deactivated == 1
    found, _ = products.search_products(conn, vendor.id, "B")
    assert found[0].active is False
    # Re-importing B reactivates it.
    r = products.sync_products(conn, vendor.id, [item("B")])
    assert r.updated == 1
    assert products.search_products(conn, vendor.id, "B")[0][0].active is True


def test_sync_without_deactivate_leaves_others(conn, vendor):
    products.sync_products(conn, vendor.id, [item("A"), item("B")])
    r = products.sync_products(conn, vendor.id, [item("A")])
    assert r.deactivated == 0
    assert products.search_products(conn, vendor.id, "B")[0][0].active is True


def test_sync_rejects_duplicate_sku_and_rolls_back(conn, vendor):
    with pytest.raises(ProductError, match="Duplicate"):
        products.sync_products(conn, vendor.id, [item("A"), item("A")])
    assert products.count_products(conn, vendor.id) == 0


def test_products_are_scoped_per_vendor(conn, vendor):
    other, _ = vendors.create_vendor(conn, "Other")
    products.sync_products(conn, vendor.id, [item("A")])
    products.sync_products(conn, other.id, [item("A", price="9.00")], deactivate_missing=True)
    assert products.search_products(conn, vendor.id)[0][0].unit_price == "1.00"
    assert products.count_products(conn, other.id) == 1


def test_search_escapes_like_wildcards(conn, vendor):
    products.sync_products(conn, vendor.id, [item("A_1"), item("AB1"), item("100%")])
    assert [p.sku for p in products.search_products(conn, vendor.id, "A_")[0]] == ["A_1"]
    assert [p.sku for p in products.search_products(conn, vendor.id, "%")[0]] == ["100%"]


def test_search_pagination(conn, vendor):
    products.sync_products(conn, vendor.id, [item(f"S{i:03}") for i in range(7)])
    page2, total = products.search_products(conn, vendor.id, page=2, page_size=3)
    assert total == 7
    assert [p.sku for p in page2] == ["S003", "S004", "S005"]


# --- CSV source -------------------------------------------------------------


def parse(text: str, encoding="utf-8"):
    return sources.CsvSource(text.encode(encoding)).fetch()


def test_csv_parses_rows_with_bom_and_case_insensitive_headers():
    r = parse("﻿SKU,Name,Unit_Price,Color\r\nA,Widget,1.50,blue\r\n\r\nB,Gadget,2,red\r\n")
    assert r.ok
    assert [p.sku for p in r.items] == ["A", "B"]
    assert r.ignored_columns == ["color"]


def test_csv_quoted_fields():
    r = parse(HEADER + 'A,"Widget, large ""XL""",1.00,USD,EA\r\n')
    assert r.items[0].name == 'Widget, large "XL"'


def test_csv_missing_required_columns():
    r = parse("sku,name\r\nA,Widget\r\n")
    assert r.errors == ["Missing required column(s): unit_price"]


def test_csv_row_errors_have_line_numbers():
    r = parse(HEADER + "A,Widget,1,USD,EA\r\nB,,x,USD,EA\r\nA,Dup,1,USD,EA\r\n")
    assert not r.ok
    assert r.errors[0].startswith("Line 3: name is required")
    assert "Line 4: duplicate SKU 'A' (first on line 2)" in r.errors


def test_csv_extra_values():
    assert parse(HEADER + "A,Widget,1,USD,EA,extra\r\n").errors == ["Line 2: more values than columns"]


def test_csv_not_utf8():
    assert "not UTF-8" in parse(HEADER + "A,Café,1,USD,EA\r\n", encoding="latin-1").errors[0]


def test_csv_empty():
    assert parse("").errors == ["File is empty."]
    assert parse(HEADER).errors == ["File has no product rows."]


def test_csv_template_round_trips():
    r = sources.CsvSource(sources.csv_template().encode()).fetch()
    assert r.ok and r.items[0].sku == "WID-100" and not r.ignored_columns


# --- admin pages ------------------------------------------------------------


def make_vendor(client) -> str:
    r = client.post("/admin/vendors", data={"name": "Acme"})
    return re.search(r"/admin/vendors/(v[0-9a-f]{12})/rotate-secret", r.text).group(1)


def upload(client, vendor_id, text, **data):
    return client.post(
        f"/admin/vendors/{vendor_id}/products/import",
        files={"file": ("products.csv", text.encode(), "text/csv")},
        data=data,
    )


def test_import_and_list(client):
    login(client)
    vid = make_vendor(client)
    r = upload(client, vid, HEADER + "A,Blue widget,1.50,USD,EA\r\nB,Red <b>gadget</b>,2,USD,BX\r\n")
    assert r.status_code == 200
    assert "2 created, 0 updated, 0 unchanged" in r.text

    listing = client.get(f"/admin/vendors/{vid}/products")
    assert "Blue widget" in listing.text
    assert "Red &lt;b&gt;gadget&lt;/b&gt;" in listing.text  # escaped
    assert "2 products" in listing.text
    assert "2 products" in client.get(f"/admin/vendors/{vid}").text

    assert "Blue widget" not in client.get(f"/admin/vendors/{vid}/products?q=red").text


def test_import_with_errors_imports_nothing(client):
    login(client)
    vid = make_vendor(client)
    r = upload(client, vid, HEADER + "A,Widget,1,USD,EA\r\nB,Gadget,oops,USD,EA\r\n")
    assert r.status_code == 422
    assert "Nothing was imported" in r.text
    assert "Line 3" in r.text
    assert "No products yet" in client.get(f"/admin/vendors/{vid}/products").text


def test_import_full_sync_checkbox(client):
    login(client)
    vid = make_vendor(client)
    upload(client, vid, HEADER + "A,Widget,1,USD,EA\r\nB,Gadget,1,USD,EA\r\n")
    r = upload(client, vid, HEADER + "A,Widget,1,USD,EA\r\n", deactivate_missing="true")
    assert "1 deactivated" in r.text


def test_template_download(client):
    login(client)
    vid = make_vendor(client)
    r = client.get(f"/admin/vendors/{vid}/products/template.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("sku,name,unit_price")


def test_product_pages_require_admin_and_known_vendor(client):
    assert client.get("/admin/vendors/vnope/products").status_code == 401
    login(client)
    assert client.get("/admin/vendors/vnope/products").status_code == 404
    assert upload(client, "vnope", HEADER).status_code == 404
