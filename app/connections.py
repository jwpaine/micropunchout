import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.cxml.common import Credential


class InvalidConnection(ValueError):
    pass


@dataclass(frozen=True)
class Connection:
    id: int
    vendor_pk: int
    name: str
    from_domain: str
    from_identity: str
    active: bool
    created_at: str
    # Settings overrides merged over the vendor's; see app.punchout_config.
    config: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Connection":
        return cls(
            **{k: row[k] for k in row.keys()}
            | {"active": bool(row["active"]), "config": json.loads(row["config"])}
        )


def list_connections(conn: sqlite3.Connection, vendor_pk: int) -> list[Connection]:
    rows = conn.execute("SELECT * FROM connections WHERE vendor_pk = ? ORDER BY name", (vendor_pk,))
    return [Connection.from_row(r) for r in rows]


def get_connection(conn: sqlite3.Connection, vendor_pk: int, connection_pk: int) -> Connection | None:
    row = conn.execute(
        "SELECT * FROM connections WHERE vendor_pk = ? AND id = ?", (vendor_pk, connection_pk)
    ).fetchone()
    return Connection.from_row(row) if row else None


def create_connection(
    conn: sqlite3.Connection, vendor_pk: int, name: str, from_domain: str, from_identity: str
) -> Connection:
    name, from_domain, from_identity = name.strip(), from_domain.strip(), from_identity.strip()
    if not name:
        raise InvalidConnection("Buyer name is required.")
    if not from_domain or not from_identity:
        raise InvalidConnection("Credential domain and identity are required.")
    if max(len(name), len(from_domain), len(from_identity)) > 200:
        raise InvalidConnection("Values must be at most 200 characters.")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO connections (vendor_pk, name, from_domain, from_identity, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (vendor_pk, name, from_domain, from_identity, datetime.now(UTC).isoformat(timespec="seconds")),
            )
    except sqlite3.IntegrityError:
        raise InvalidConnection(
            f"A connection for {from_domain} {from_identity} already exists for this vendor."
        ) from None
    return get_connection(conn, vendor_pk, cur.lastrowid)


def set_active(conn: sqlite3.Connection, vendor_pk: int, connection_pk: int, active: bool) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE connections SET active = ? WHERE vendor_pk = ? AND id = ?", (int(active), vendor_pk, connection_pk)
        )
    return cur.rowcount == 1


def find_by_credentials(conn: sqlite3.Connection, vendor_pk: int, creds: list[Credential]) -> Connection | None:
    """Find the active connection matching any of the request's From credentials (case-insensitive)."""
    for cred in creds:
        row = conn.execute(
            "SELECT * FROM connections WHERE vendor_pk = ? AND from_domain = ? AND from_identity = ? AND active = 1",
            (vendor_pk, cred.domain, cred.identity),
        ).fetchone()
        if row:
            return Connection.from_row(row)
    return None


def update_config(conn: sqlite3.Connection, vendor_pk: int, connection_pk: int, overrides: dict) -> bool:
    """Store already-validated settings overrides (see punchout_config.parse_overrides)."""
    with conn:
        cur = conn.execute(
            "UPDATE connections SET config = ? WHERE vendor_pk = ? AND id = ?",
            (json.dumps(overrides, sort_keys=True), vendor_pk, connection_pk),
        )
    return cur.rowcount == 1
