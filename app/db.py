import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing

from fastapi import Request

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id                INTEGER PRIMARY KEY,
    vendor_id         TEXT NOT NULL UNIQUE,              -- public ID used in punchout URLs
    name              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    secret_hash       TEXT NOT NULL,                     -- sha256 of the shared secret
    secret_last4      TEXT NOT NULL,                     -- shown in the UI to identify the secret
    created_at        TEXT NOT NULL,
    secret_rotated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id                   INTEGER PRIMARY KEY,
    vendor_pk            INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    sku                  TEXT NOT NULL,
    name                 TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    unit_price           TEXT NOT NULL,                  -- decimal string, e.g. "12.50"; never float
    currency             TEXT NOT NULL,                  -- ISO 4217, e.g. "USD"
    uom                  TEXT NOT NULL,                  -- UN/CEFACT unit code, e.g. "EA"
    unspsc               TEXT NOT NULL DEFAULT '',
    manufacturer_name    TEXT NOT NULL DEFAULT '',
    manufacturer_part_id TEXT NOT NULL DEFAULT '',
    image_url            TEXT NOT NULL DEFAULT '',
    lead_time_days       INTEGER,
    active               INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (vendor_pk, sku)
);

-- A buyer organization that punches out to a vendor, identified by the cXML From credential.
CREATE TABLE IF NOT EXISTS connections (
    id            INTEGER PRIMARY KEY,
    vendor_pk     INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    from_domain   TEXT NOT NULL COLLATE NOCASE,          -- e.g. NetworkID, DUNS
    from_identity TEXT NOT NULL COLLATE NOCASE,          -- e.g. AN01000000001
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    UNIQUE (vendor_pk, from_domain, from_identity)
);

CREATE TABLE IF NOT EXISTS punchout_sessions (
    id                    INTEGER PRIMARY KEY,
    vendor_pk             INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    connection_pk         INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    start_token           TEXT NOT NULL UNIQUE,          -- one-time, in the StartPage URL
    shop_token            TEXT NOT NULL UNIQUE,          -- in shopping page URLs
    status                TEXT NOT NULL,                 -- pending | active | returned
    operation             TEXT NOT NULL,                 -- create | edit | inspect
    buyer_cookie          TEXT NOT NULL,
    browser_form_post_url TEXT NOT NULL,
    user_external_id      TEXT NOT NULL DEFAULT '',
    user_email            TEXT NOT NULL DEFAULT '',
    user_name             TEXT NOT NULL DEFAULT '',
    ship_to_id            TEXT NOT NULL DEFAULT '',
    payload_id            TEXT NOT NULL,
    raw_request           TEXT NOT NULL,
    from_credential       TEXT NOT NULL,                 -- JSON: buyer's From, echoed as To in the POOM
    to_credential         TEXT NOT NULL,                 -- JSON: supplier's To, echoed as From in the POOM
    created_at            TEXT NOT NULL,
    start_expires_at      TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    started_at            TEXT,
    returned_at           TEXT
);
CREATE INDEX IF NOT EXISTS punchout_sessions_vendor ON punchout_sessions (vendor_pk, created_at);

CREATE TABLE IF NOT EXISTS cart_lines (
    id          INTEGER PRIMARY KEY,
    session_pk  INTEGER NOT NULL REFERENCES punchout_sessions(id) ON DELETE CASCADE,
    product_pk  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE (session_pk, product_pk)
);

-- A shopper account, created or updated from the mapped setup request fields on every punchout.
CREATE TABLE IF NOT EXISTS shoppers (
    id               INTEGER PRIMARY KEY,
    vendor_pk        INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    connection_pk    INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    external_id      TEXT NOT NULL COLLATE NOCASE,       -- mapped user.external_id
    email            TEXT NOT NULL DEFAULT '',
    name             TEXT NOT NULL DEFAULT '',
    attributes       TEXT NOT NULL DEFAULT '{}',         -- JSON: every other mapped field
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_punchout_at TEXT NOT NULL,
    UNIQUE (connection_pk, external_id)
);

-- Every PunchOutOrderMessage sent back to a procurement system, exactly as sent.
CREATE TABLE IF NOT EXISTS order_messages (
    id          INTEGER PRIMARY KEY,
    session_pk  INTEGER NOT NULL REFERENCES punchout_sessions(id) ON DELETE CASCADE,
    payload_id  TEXT NOT NULL,
    total       TEXT NOT NULL,
    currency    TEXT NOT NULL,
    line_count  INTEGER NOT NULL,
    cxml        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


# Changes to tables that already exist. SCHEMA above creates the original tables; each entry
# here runs once, in order, tracked by SQLite's user_version. Append only; never edit or reorder.
def _upgrade_vendor_settings(conn: sqlite3.Connection) -> None:
    """Convert vendor settings saved before field mappings existed (fields/description/classification)."""
    for vendor_pk, config in conn.execute("SELECT id, config FROM vendors").fetchall():
        old = json.loads(config)
        new = {k: v for k, v in old.items() if k in ("punchout", "return")}
        posr = {f"user.{field}": {"from": sources} for field, sources in old.get("fields", {}).get("user", {}).items()}
        poom: dict = {}
        description = old.get("description", {})
        if "fields" in description or description.get("max_length"):
            parts = [f"product:{f}" for f in description.get("fields", ["name"])]
            rule: dict = {"from": [" + ".join(parts), "product:name"], "separator": " - "}
            if description.get("max_length"):
                rule["transform"] = [f"truncate:{description['max_length']}"]
            poom["item.description"] = rule
        classification = old.get("classification", {})
        if classification.get("domain"):
            poom["item.classification_domain"] = {"from": [f"value:{classification['domain']}"]}
        if classification.get("default"):
            poom["item.classification"] = {"from": ["product:unspsc", f"value:{classification['default']}"]}
        if posr:
            new["posr"] = posr
        if poom:
            new["poom"] = poom
        if new != old:
            conn.execute("UPDATE vendors SET config = ? WHERE id = ?", (json.dumps(new, sort_keys=True), vendor_pk))


def _redact_stored_secrets(conn: sqlite3.Connection) -> None:
    secret = re.compile(r"(<SharedSecret\b[^>]*>)(.*?)(</SharedSecret>)", re.DOTALL)
    for session_pk, raw in conn.execute("SELECT id, raw_request FROM punchout_sessions").fetchall():
        redacted = secret.sub(r"\g<1>****\g<3>", raw)
        if redacted != raw:
            conn.execute("UPDATE punchout_sessions SET raw_request = ? WHERE id = ?", (redacted, session_pk))


MIGRATIONS: list[str | Callable[[sqlite3.Connection], None]] = [
    # 1: per-vendor settings overrides as a JSON object (becomes jsonb on Postgres).
    "ALTER TABLE vendors ADD COLUMN config TEXT NOT NULL DEFAULT '{}'",
    # 2: per-connection settings overrides, merged over the vendor's.
    "ALTER TABLE connections ADD COLUMN config TEXT NOT NULL DEFAULT '{}'",
    # 3: the mapped setup request fields for each session (JSON).
    "ALTER TABLE punchout_sessions ADD COLUMN fields TEXT NOT NULL DEFAULT '{}'",
    # 4: the shopper account each session belongs to.
    "ALTER TABLE punchout_sessions ADD COLUMN shopper_pk INTEGER REFERENCES shoppers(id)",
    # 5: old settings keys to field mappings.
    _upgrade_vendor_settings,
    # 6: shared secrets were stored in raw setup requests; replace them with ****.
    _redact_stored_secrets,
]


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            if callable(step):
                step(conn)
            else:
                conn.execute(step)
            conn.execute(f"PRAGMA user_version = {number}")


def connect(path: str) -> sqlite3.Connection:
    # Each request gets its own connection, but FastAPI may run the dependency and
    # the endpoint on different threadpool threads, so allow cross-thread use.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str) -> None:
    with closing(connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        apply_schema(conn)


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    with closing(connect(request.app.state.settings.database_path)) as conn:
        yield conn
