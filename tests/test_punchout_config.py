import base64
import html
import json
import re
import sqlite3

import pytest
from lxml import etree

from app import punchout_config
from app.cxml.setup import build_setup_request, parse_setup_request
from app.db import MIGRATIONS, SCHEMA, init_db
from app.mapping import PoomRule, PosrRule, map_fields, resolve
from app.punchout_config import ConfigError, effective, parse_overrides
from tests.test_punchout import (  # noqa: F401  (setup is a fixture)
    BUYER,
    RETURN_URL,
    posr,
    product_id,
    send_setup,
    setup,
    start_shopping,
)


def lookup_from(values: dict[str, str]):
    return lambda source: values.get(source, "")


# --- mapping engine ---------------------------------------------------------


def test_first_non_empty_source_wins():
    rule = PosrRule.model_validate({"from": ["extrinsic:A", "extrinsic:B", "extrinsic:C"]})
    assert resolve(rule, lookup_from({"extrinsic:B": "b", "extrinsic:C": "c"})) == "b"


def test_combined_parts_use_separator_and_skip_empties():
    rule = PosrRule.model_validate({"from": ["extrinsic:First + extrinsic:Middle + extrinsic:Last"]})
    assert resolve(rule, lookup_from({"extrinsic:First": "Jane", "extrinsic:Last": "Smith"})) == "Jane Smith"
    rule = PoomRule.model_validate({"from": ["product:sku + product:name"], "separator": " - "})
    assert resolve(rule, lookup_from({"product:sku": "A", "product:name": "Widget"})) == "A - Widget"


def test_constant_map_transform_and_default():
    rule = PosrRule.model_validate({"from": ["value:Hello"], "transform": ["upper"]})
    assert resolve(rule, lookup_from({})) == "HELLO"

    rule = PosrRule.model_validate({"from": ["shipto:address_id"], "map": {"1000": "C-1"}})
    assert resolve(rule, lookup_from({"shipto:address_id": "1000"})) == "C-1"
    assert resolve(rule, lookup_from({"shipto:address_id": "9999"})) == "9999"  # unmatched passes through

    rule = PosrRule.model_validate({"from": ["extrinsic:X"], "transform": ["trim", "truncate:5"], "default": "n/a"})
    assert resolve(rule, lookup_from({"extrinsic:X": "  abcdefgh "})) == "abcde"
    assert resolve(rule, lookup_from({})) == "n/a"


def test_required_fields_reported_missing():
    rules = {
        "a": PosrRule.model_validate({"from": ["extrinsic:A"], "required": True}),
        "b": PosrRule.model_validate({"from": ["extrinsic:B"], "required": True}),
        "c": PosrRule.model_validate({"from": ["extrinsic:C"]}),
    }
    values, missing = map_fields(rules, lookup_from({"extrinsic:A": "x"}))
    assert values == {"a": "x", "b": "", "c": ""}
    assert missing == ["b"]


# --- setup request sources --------------------------------------------------


def request_with(extrinsics=None, inject: str = ""):
    body = build_setup_request(
        from_cred=BUYER, to_cred=BUYER, sender_cred=BUYER, shared_secret="s",
        browser_form_post_url=RETURN_URL, buyer_cookie="c", extrinsics=extrinsics or {},
    )
    if inject:
        body = body.replace(b"<SupplierSetup>", inject.encode() + b"<SupplierSetup>")
    return parse_setup_request(body)


def test_ship_to_and_from_sources():
    req = request_with(inject=(
        '<ShipTo><Address addressID="1000"><Name xml:lang="en">Acme HQ</Name><PostalAddress>'
        "<Street>1 Main St</Street><Street>Suite 5</Street><City>Springfield</City><State>IL</State>"
        '<PostalCode>62701</PostalCode><Country isoCountryCode="US">United States</Country>'
        "</PostalAddress></Address></ShipTo>"
    ))
    assert req.lookup("shipto:address_id") == "1000"
    assert req.lookup("shipto:name") == "Acme HQ"
    assert req.lookup("shipto:street") == "1 Main St, Suite 5"
    assert (req.lookup("shipto:city"), req.lookup("shipto:state"), req.lookup("shipto:postal_code")) == (
        "Springfield", "IL", "62701")
    assert req.lookup("shipto:country") == "US"
    assert req.lookup("from:identity") == BUYER.identity
    assert req.lookup("from:domain") == BUYER.domain


def test_default_user_mapping():
    req = request_with({"FirstName": "Jane", "LastName": "Smith", "UserId": "u-42", "UserEmail": "j@b.io"})
    fields, missing = map_fields(effective().posr_rules(), req.lookup)
    assert not missing
    assert fields["user.external_id"] == "u-42"
    assert fields["user.email"] == "j@b.io"
    assert fields["user.name"] == "Jane Smith"


def test_contact_prefers_end_user():
    req = request_with(inject=(
        '<Contact role="buyer"><Name xml:lang="en">Boss</Name><Email>boss@b.io</Email></Contact>'
        '<Contact role="endUser"><Name xml:lang="en">Sam</Name><Email>sam@b.io</Email></Contact>'
    ))
    assert (req.lookup("contact:name"), req.lookup("contact:email")) == ("Sam", "sam@b.io")


# --- validation -------------------------------------------------------------


def test_empty_means_no_overrides():
    assert parse_overrides("") == {}


@pytest.mark.parametrize(
    "text, message",
    [
        ("{not json", "Not valid JSON"),
        ("[1]", "must be a JSON object"),
        ('{"retrun": {}}', "retrun: unknown setting"),
        ('{"return": {"target": "_blank"}}', "return.target"),
        ('{"punchout": {"base_url": "punchout.example.com"}}', "punchout.base_url"),
        ('{"posr": {"user.email": {"from": ["UserEmail"]}}}', "is not a valid source"),
        ('{"posr": {"user.email": {"from": ["product:sku"]}}}', "is not a valid source"),
        ('{"posr": {"user.email": {"from": []}}}', "posr.user.email.from"),
        ('{"posr": {"user.email": {"from": ["contact:email"], "transform": ["shout"]}}}', "not a valid transform"),
        ('{"posr": {"user.email": {"form": ["contact:email"]}}}', "posr.user.email.form: unknown setting"),
        ('{"posr": {"Cost Center": {"from": ["value:x"]}}}', "is not a valid field name"),
        ('{"posr": {"user.external_id": null}}', "can't be switched off"),
        ('{"poom": {"item.colour": {"from": ["value:x"]}}}', "is not a cart field"),
        ('{"poom": {"item.description": {"from": ["extrinsic:X"]}}}', "is not a valid source"),
        ('{"poom": {"item.extrinsic.": {"from": ["value:x"]}}}', "is not a cart field"),
    ],
)
def test_invalid_overrides(text, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse_overrides(text)


def test_partial_rule_merges_and_null_switches_off():
    config = effective({"posr": {"user.email": {"transform": ["lower"]}, "ship_to.id": None}})
    rules = config.posr_rules()
    assert rules["user.email"].from_ == ["extrinsic:UserEmail", "contact:email"]  # kept from defaults
    assert rules["user.email"].transform == ["lower"]
    assert "ship_to.id" not in rules


def test_connection_layer_wins_over_vendor():
    vendor = {"return": {"target": "_top"}, "poom": {"item.extrinsic.Contract": {"from": ["value:V"]}}}
    connection = {"poom": {"item.extrinsic.Contract": {"from": ["value:C"]}}}
    config = effective(vendor, connection)
    assert config.return_.target == "_top"
    assert config.poom_rules()["item.extrinsic.Contract"].from_ == ["value:C"]

    rows = {r["path"]: r["source"] for r in punchout_config.explain(("vendor", vendor), ("connection", connection))}
    assert rows["return.target"] == "vendor"
    assert rows["poom.item.extrinsic.Contract"] == "connection"
    assert rows["poom.item.description"] == "default"


def test_connection_overrides_are_validated_against_vendor_layer():
    vendor = {"posr": {"cost_center": {"from": ["extrinsic:CostCenter"]}}}
    # A partial rule is fine because the vendor layer supplies "from".
    assert parse_overrides('{"posr": {"cost_center": {"required": true}}}', vendor)
    # Without the vendor layer the same partial rule has no sources.
    with pytest.raises(ConfigError, match="posr.cost_center.from"):
        parse_overrides('{"posr": {"cost_center": {"required": true}}}')


# --- migration of old settings keys -----------------------------------------


def test_migration_converts_old_settings(tmp_path):
    path = tmp_path / "old.db"
    old_config = {
        "return": {"target": "_top"},
        "fields": {"user": {"email": ["extrinsic:Mail"]}},
        "description": {"fields": ["sku", "name"], "max_length": 40},
        "classification": {"domain": "custom", "default": "MISC"},
    }
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("ALTER TABLE vendors ADD COLUMN config TEXT NOT NULL DEFAULT '{}'")
        db.execute("PRAGMA user_version = 1")
        db.execute(
            "INSERT INTO vendors (vendor_id, name, secret_hash, secret_last4, created_at, secret_rotated_at, config)"
            " VALUES ('vold', 'Old Co', 'h', 'abcd', 'now', 'now', ?)",
            (json.dumps(old_config),),
        )
    init_db(str(path))
    init_db(str(path))  # no-op the second time

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        config = json.loads(db.execute("SELECT config FROM vendors").fetchone()[0])
    assert config == {
        "return": {"target": "_top"},
        "posr": {"user.email": {"from": ["extrinsic:Mail"]}},
        "poom": {
            "item.description": {
                "from": ["product:sku + product:name", "product:name"], "separator": " - ",
                "transform": ["truncate:40"],
            },
            "item.classification_domain": {"from": ["value:custom"]},
            "item.classification": {"from": ["product:unspsc", "value:MISC"]},
        },
    }
    parse_overrides(json.dumps(config))  # valid under the new schema


# --- setup: required fields and shopper accounts ----------------------------


def save_settings(client, settings_url: str, config: dict):
    """Save a full settings object the way the admin does: one section at a time."""
    parts = {
        "general": {k: config[k] for k in ("punchout", "return") if k in config},
        "posr": config.get("posr"),
        "poom": config.get("poom"),
    }
    for section, part in parts.items():
        if part:
            r = client.post(f"{settings_url}/{section}", data={"config": json.dumps(part)}, follow_redirects=False)
            assert r.status_code == 303, r.text


def save_vendor_settings(client, vendor_id, config: dict):
    save_settings(client, f"/admin/vendors/{vendor_id}/settings", config)


def test_missing_required_field_rejects_setup(client, setup):
    vendor_id, secret = setup
    save_vendor_settings(client, vendor_id, {"posr": {"cost_center": {"from": ["extrinsic:CostCenter"], "required": True}}})
    code, message, _ = send_setup(client, vendor_id, posr(secret))
    assert (code, message) == (400, "Missing required field(s): cost_center")


def test_custom_fields_are_stored_on_session_and_shopper(client, setup):
    vendor_id, secret = setup
    save_vendor_settings(client, vendor_id, {
        "posr": {
            "user.email": {"transform": ["upper"]},
            "account.id": {"from": ["extrinsic:Dept"], "map": {"D1": "ACCT-1"}},
            "cost_center": {"from": ["extrinsic:CostCenter"], "default": "none"},
        }
    })
    body = posr(secret).replace(
        b"<BrowserFormPost>",
        b'<Extrinsic name="Dept">D1</Extrinsic><BrowserFormPost>',
    )
    assert send_setup(client, vendor_id, body)[0] == 200

    page = client.get(f"/admin/vendors/{vendor_id}/shoppers").text
    assert "id jsmith" in page and "JANE@BUYER.EXAMPLE" in page
    assert "ACCT-1" in page and "cost_center" in page

    vendor_page = client.get(f"/admin/vendors/{vendor_id}").text
    session_pk = re.search(rf"/admin/vendors/{vendor_id}/sessions/(\d+)", vendor_page).group(1)
    detail = html.unescape(client.get(f"/admin/vendors/{vendor_id}/sessions/{session_pk}").text)
    assert '"account.id": "ACCT-1"' in detail
    assert '"cost_center": "none"' in detail


def test_shopper_is_updated_not_duplicated(client, setup, tmp_path):
    vendor_id, secret = setup
    send_setup(client, vendor_id, posr(secret))
    send_setup(client, vendor_id, posr(secret).replace(b"Jane Smith", b"Jane Doe"))
    with sqlite3.connect(tmp_path / "test.db") as db:
        rows = db.execute("SELECT external_id, name FROM shoppers").fetchall()
        sessions = db.execute("SELECT COUNT(DISTINCT shopper_pk), COUNT(*) FROM punchout_sessions").fetchone()
    assert rows == [("jsmith", "Jane Doe")]
    assert sessions == (1, 2)
    assert "1 shopper" in client.get(f"/admin/vendors/{vendor_id}").text


# --- cart (POOM) mappings ---------------------------------------------------


def checkout_poom(client, shop) -> etree._Element:
    r = client.post(f"{shop}/checkout")
    assert r.status_code == 200, r.text
    value = re.search(r'name="cxml-(urlencoded|base64)" value="([^"]*)"', r.text)
    raw = html.unescape(value.group(2))
    return etree.fromstring(base64.b64decode(raw) if value.group(1) == "base64" else raw.encode())


def test_poom_mappings(client, setup):
    vendor_id, secret = setup
    save_vendor_settings(client, vendor_id, {
        "posr": {"cost_center": {"from": ["extrinsic:UniqueName"], "transform": ["upper"]}},
        "poom": {
            "item.description": {"from": ["product:sku + product:name"], "separator": " - ", "transform": ["truncate:8"]},
            "item.supplier_part_aux_id": {"from": ["value:CFG + product:sku"], "separator": "-"},
            "item.classification": {"from": ["product:unspsc", "value:44120000"]},
            "item.classification_domain": {"from": ["value:UNSPSC_V26"]},
            "item.lead_time": {"from": ["value:soon"]},
            "item.extrinsic.CostCenter": {"from": ["session:cost_center"]},
            "item.extrinsic.Contract": {"from": ["value:C-2291"]},
            "item.extrinsic.Empty": {"from": ["session:missing"]},
        },
    })
    shop = start_shopping(client, vendor_id, secret)
    client.post(f"{shop}/cart/add", data={"product_id": product_id(client, shop, "A"), "quantity": 1})
    client.post(f"{shop}/cart/add", data={"product_id": product_id(client, shop, "B"), "quantity": 1})
    root = checkout_poom(client, shop)

    a, b = root.findall(".//ItemIn")
    assert a.findtext("ItemID/SupplierPartAuxiliaryID") == "CFG-A"
    assert a.findtext("ItemDetail/Description") == "A - Widg"
    assert a.findtext("ItemDetail/Classification") == "31160000"
    assert b.findtext("ItemDetail/Classification") == "44120000"  # B has no UNSPSC
    assert a.find("ItemDetail/Classification").get("domain") == "UNSPSC_V26"
    assert a.find("ItemDetail/LeadTime") is None  # not a whole number
    extrinsics = {e.get("name"): e.text for e in a.findall("ItemDetail/Extrinsic")}
    assert extrinsics == {"CostCenter": "JSMITH", "Contract": "C-2291"}  # empty ones are left out
    # ItemDetail children stay in DTD order.
    assert [c.tag for c in a.find("ItemDetail")] == [
        "UnitPrice", "Description", "UnitOfMeasure", "Classification", "Extrinsic", "Extrinsic",
    ]


def test_required_poom_field_blocks_checkout(client, setup):
    vendor_id, secret = setup
    save_vendor_settings(client, vendor_id, {"poom": {"item.classification": {"required": True}}})
    shop = start_shopping(client, vendor_id, secret)
    client.post(f"{shop}/cart/add", data={"product_id": product_id(client, shop, "B"), "quantity": 1})
    r = client.post(f"{shop}/checkout")
    assert r.status_code == 409
    assert "B: no value for item.classification" in r.text
    assert client.get(shop).status_code == 200  # session still open


def test_return_settings(client, setup):
    vendor_id, secret = setup
    save_vendor_settings(client, vendor_id, {"return": {"form_field": "cxml-base64", "target": "_top"}})
    shop = start_shopping(client, vendor_id, secret)
    r = client.post(f"{shop}/checkout")
    assert 'target="_top"' in re.search(r'<form id="return-form"[^>]*>', r.text).group(0)
    assert 'name="cxml-base64"' in r.text


def test_defaults_without_settings(client, setup):
    vendor_id, secret = setup
    shop = start_shopping(client, vendor_id, secret)
    client.post(f"{shop}/cart/add", data={"product_id": product_id(client, shop, "B"), "quantity": 1})
    root = checkout_poom(client, shop)
    item = root.find(".//ItemIn")
    assert item.find("ItemID/SupplierPartAuxiliaryID") is None
    assert item.findtext("ItemDetail/Description") == "Gadget"
    assert item.find("ItemDetail/Classification").get("domain") == "UNSPSC"
    assert item.find("ItemDetail/Extrinsic") is None


# --- connection settings ----------------------------------------------------


def connection_pk(client, vendor_id) -> str:
    page = client.get(f"/admin/vendors/{vendor_id}").text
    return re.search(rf"/admin/vendors/{vendor_id}/connections/(\d+)/settings", page).group(1)


def test_connection_settings_apply_only_to_that_buyer(client, setup):
    vendor_id, secret = setup
    cid = connection_pk(client, vendor_id)
    save_settings(client, f"/admin/vendors/{vendor_id}/connections/{cid}/settings", {
        "punchout": {"base_url": "https://buyer-specific.example.com"},
        "poom": {"item.extrinsic.Contract": {"from": ["value:GLOBEX-7"]}},
    })
    vendor_page = client.get(f"/admin/vendors/{vendor_id}").text
    assert "custom general" in vendor_page and "POOM</a> <span class=\"badge ok\">custom" in vendor_page

    code, _, start_page = send_setup(client, vendor_id, posr(secret))
    assert code == 200 and start_page.startswith("https://buyer-specific.example.com/punchout/start/")

    editor = client.get(f"/admin/vendors/{vendor_id}/connections/{cid}/settings/poom").text
    assert re.search(r"item\.extrinsic\.Contract</code>.*?badge ok\">connection", editor, re.S)


def test_connection_settings_validation(client, setup):
    vendor_id, _ = setup
    cid = connection_pk(client, vendor_id)
    r = client.post(
        f"/admin/vendors/{vendor_id}/connections/{cid}/settings/posr",
        data={"config": '{"user.external_id": null}'},
    )
    assert r.status_code == 422
    assert "can&#39;t be switched off" in r.text


def test_vendor_save_rejected_if_it_breaks_a_connection(client, setup):
    vendor_id, _ = setup
    cid = connection_pk(client, vendor_id)
    save_vendor_settings(client, vendor_id, {"posr": {"cost_center": {"from": ["extrinsic:CostCenter"]}}})
    client.post(
        f"/admin/vendors/{vendor_id}/connections/{cid}/settings/posr",
        data={"config": '{"cost_center": {"required": true}}'},
    )
    # Removing the vendor's cost_center mapping would leave the connection's partial rule without sources.
    r = client.post(f"/admin/vendors/{vendor_id}/settings/posr", data={"config": "{}"})
    assert r.status_code == 422
    assert "Conflicts with connection" in r.text


def test_settings_pages_require_admin(client):
    for path in ("/settings", "/settings/posr", "/connections/1/settings", "/connections/1/settings/poom", "/shoppers"):
        assert client.get(f"/admin/vendors/vx{path}", headers={"accept": "application/json"}).status_code == 401


# --- admin: POSR and POOM are edited separately -----------------------------


def test_settings_root_redirects_to_general(client, setup):
    vendor_id, _ = setup
    r = client.get(f"/admin/vendors/{vendor_id}/settings", follow_redirects=False)
    assert r.headers["location"] == f"/admin/vendors/{vendor_id}/settings/general"
    assert client.get(f"/admin/vendors/{vendor_id}/settings/bogus").status_code == 404


def test_each_tab_shows_only_its_own_settings(client, setup):
    vendor_id, _ = setup
    base = f"/admin/vendors/{vendor_id}/settings"
    posr_page, poom_page, general_page = (client.get(f"{base}/{s}").text for s in ("posr", "poom", "general"))

    assert "user.external_id" in posr_page and "item.description" not in posr_page.split("<h2>Reference")[0]
    assert "item.description" in poom_page and "user.external_id" not in poom_page.split("<h2>Reference")[0]
    assert "return.target" in general_page and "user.external_id" not in general_page
    assert 'aria-current="page">Setup request mapping (POSR)' in posr_page
    assert 'aria-current="page">Cart mapping (POOM)' in poom_page


def test_saving_one_section_keeps_the_others(client, setup, tmp_path):
    vendor_id, _ = setup
    base = f"/admin/vendors/{vendor_id}/settings"
    client.post(f"{base}/general", data={"config": '{"return": {"target": "_top"}}'})
    client.post(f"{base}/posr", data={"config": '{"cost_center": {"from": ["extrinsic:CostCenter"]}}'})
    r = client.post(f"{base}/poom", data={"config": '{"item.supplier_part_aux_id": {"from": ["product:sku"]}}'},
                    follow_redirects=False)
    assert r.headers["location"] == f"{base}/poom?saved=1"

    with sqlite3.connect(tmp_path / "test.db") as db:
        config = json.loads(db.execute("SELECT config FROM vendors").fetchone()[0])
    assert config == {
        "return": {"target": "_top"},
        "posr": {"cost_center": {"from": ["extrinsic:CostCenter"]}},
        "poom": {"item.supplier_part_aux_id": {"from": ["product:sku"]}},
    }

    # Each editor shows only its section's overrides.
    editor = client.get(f"{base}/posr?saved=1").text
    textarea = html.unescape(re.search(r"<textarea[^>]*>(.*?)</textarea>", editor, re.S).group(1))
    assert json.loads(textarea) == {"cost_center": {"from": ["extrinsic:CostCenter"]}}
    assert "Saved." in editor

    # Clearing one section removes only that section.
    client.post(f"{base}/posr", data={"config": "{}"})
    with sqlite3.connect(tmp_path / "test.db") as db:
        config = json.loads(db.execute("SELECT config FROM vendors").fetchone()[0])
    assert set(config) == {"return", "poom"}


def test_mappings_rejected_on_general_tab(client, setup):
    vendor_id, _ = setup
    r = client.post(f"/admin/vendors/{vendor_id}/settings/general", data={"config": '{"posr": {}}'})
    assert r.status_code == 422
    assert "not a general setting" in r.text


def test_wrong_tab_rule_is_rejected(client, setup):
    vendor_id, _ = setup
    # A cart element on the setup request tab, and a setup source on the cart tab.
    r = client.post(f"/admin/vendors/{vendor_id}/settings/posr",
                    data={"config": '{"item.description": {"from": ["product:name"]}}'})
    assert r.status_code == 422 and "is not a valid source" in r.text
    r = client.post(f"/admin/vendors/{vendor_id}/settings/poom",
                    data={"config": '{"item.description": {"from": ["extrinsic:Desc"]}}'})
    assert r.status_code == 422 and "is not a valid source" in r.text


def test_vendor_page_groups_settings(client, setup):
    vendor_id, _ = setup
    page = client.get(f"/admin/vendors/{vendor_id}").text
    general = page.index(">General</h2>")
    posr_heading = page.index(">Setup request mapping (POSR)</h2>")
    poom_heading = page.index(">Cart mapping (POOM)</h2>")
    assert general < posr_heading < poom_heading
    assert "user.external_id" in page[posr_heading:poom_heading]
    assert "item.description" in page[poom_heading:]
    assert f'href="/admin/vendors/{vendor_id}/settings/posr">Edit' in page
