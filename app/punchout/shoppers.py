"""Shopper accounts, created or updated from each setup request's mapped fields."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Mapped fields stored in their own columns; everything else goes in ``attributes``.
CORE_FIELDS = ("user.external_id", "user.email", "user.name")


@dataclass(frozen=True)
class Shopper:
    id: int
    vendor_pk: int
    connection_pk: int
    external_id: str
    email: str
    name: str
    created_at: str
    updated_at: str
    last_punchout_at: str
    attributes: dict = field(default_factory=dict)
    connection_name: str = ""
    session_count: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Shopper":
        data = {k: row[k] for k in row.keys()}
        data["attributes"] = json.loads(data["attributes"])
        return cls(**data)


def upsert(conn: sqlite3.Connection, vendor_pk: int, connection_pk: int, fields: dict[str, str]) -> int:
    """Create or update the shopper identified by ``user.external_id`` within the connection. Returns its id."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    attributes = json.dumps({k: v for k, v in fields.items() if k not in CORE_FIELDS}, sort_keys=True)
    with conn:
        row = conn.execute(
            """INSERT INTO shoppers (vendor_pk, connection_pk, external_id, email, name, attributes,
                                     created_at, updated_at, last_punchout_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (connection_pk, external_id) DO UPDATE SET
                   email = excluded.email,
                   name = excluded.name,
                   attributes = excluded.attributes,
                   updated_at = CASE
                       WHEN shoppers.email IS NOT excluded.email OR shoppers.name IS NOT excluded.name
                         OR shoppers.attributes IS NOT excluded.attributes
                       THEN excluded.updated_at ELSE shoppers.updated_at END,
                   last_punchout_at = excluded.last_punchout_at
               RETURNING id""",
            (
                vendor_pk, connection_pk, fields["user.external_id"], fields.get("user.email", ""),
                fields.get("user.name", ""), attributes, now, now, now,
            ),
        ).fetchone()
    return row[0]


def list_shoppers(conn: sqlite3.Connection, vendor_pk: int, limit: int = 200) -> list[Shopper]:
    rows = conn.execute(
        """SELECT s.*, c.name AS connection_name,
                  (SELECT COUNT(*) FROM punchout_sessions p WHERE p.shopper_pk = s.id) AS session_count
           FROM shoppers s JOIN connections c ON c.id = s.connection_pk
           WHERE s.vendor_pk = ? ORDER BY s.last_punchout_at DESC LIMIT ?""",
        (vendor_pk, limit),
    ).fetchall()
    return [Shopper.from_row(r) for r in rows]


def count_shoppers(conn: sqlite3.Connection, vendor_pk: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM shoppers WHERE vendor_pk = ?", (vendor_pk,)).fetchone()[0]
