import hashlib
import hmac
import json
import secrets
import sqlite3
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Letters and digits only: some procurement platforms reject symbols in shared secret fields.
SECRET_ALPHABET = string.ascii_letters + string.digits
SECRET_LENGTH = 32
NAME_MAX_LENGTH = 200


class VendorError(ValueError):
    pass


@dataclass(frozen=True)
class Vendor:
    id: int
    vendor_id: str
    name: str
    secret_last4: str
    created_at: str
    secret_rotated_at: str
    # Settings overrides only; see app.punchout_config for defaults and merging.
    config: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Vendor":
        return cls(
            id=row["id"],
            vendor_id=row["vendor_id"],
            name=row["name"],
            secret_last4=row["secret_last4"],
            created_at=row["created_at"],
            secret_rotated_at=row["secret_rotated_at"],
            config=json.loads(row["config"]),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hash_secret(secret: str) -> str:
    # Secrets are long and random, so a fast hash is enough; no salt or KDF needed.
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_vendor_id() -> str:
    return "v" + secrets.token_hex(6)


def generate_secret() -> str:
    return "".join(secrets.choice(SECRET_ALPHABET) for _ in range(SECRET_LENGTH))


def list_vendors(conn: sqlite3.Connection) -> list[Vendor]:
    rows = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
    return [Vendor.from_row(r) for r in rows]


def get_vendor(conn: sqlite3.Connection, vendor_id: str) -> Vendor | None:
    row = conn.execute("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,)).fetchone()
    return Vendor.from_row(row) if row else None


def get_vendor_by_pk(conn: sqlite3.Connection, vendor_pk: int) -> Vendor | None:
    row = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_pk,)).fetchone()
    return Vendor.from_row(row) if row else None


def update_config(conn: sqlite3.Connection, vendor_id: str, overrides: dict) -> bool:
    """Store already-validated settings overrides (see punchout_config.parse_overrides)."""
    with conn:
        cur = conn.execute(
            "UPDATE vendors SET config = ? WHERE vendor_id = ?", (json.dumps(overrides, sort_keys=True), vendor_id)
        )
    return cur.rowcount == 1


def create_vendor(conn: sqlite3.Connection, name: str) -> tuple[Vendor, str]:
    """Create a vendor. Returns the vendor and its plaintext secret, which is not stored."""
    name = name.strip()
    if not name:
        raise VendorError("Name is required.")
    if len(name) > NAME_MAX_LENGTH:
        raise VendorError(f"Name must be at most {NAME_MAX_LENGTH} characters.")
    if conn.execute("SELECT 1 FROM vendors WHERE name = ?", (name,)).fetchone():
        raise VendorError(f"A vendor named {name!r} already exists.")

    secret = generate_secret()
    now = _now()
    for _ in range(5):
        vendor_id = generate_vendor_id()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO vendors (vendor_id, name, secret_hash, secret_last4, created_at, secret_rotated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (vendor_id, name, _hash_secret(secret), secret[-4:], now, now),
                )
        except sqlite3.IntegrityError:
            if get_vendor(conn, vendor_id) is not None:
                continue  # vendor_id collision; try another
            raise VendorError(f"A vendor named {name!r} already exists.") from None
        return get_vendor(conn, vendor_id), secret
    raise RuntimeError("Could not generate a unique vendor ID")


def rotate_secret(conn: sqlite3.Connection, vendor_id: str) -> str | None:
    """Replace a vendor's secret. Returns the new plaintext secret, or None if no such vendor."""
    secret = generate_secret()
    with conn:
        cur = conn.execute(
            "UPDATE vendors SET secret_hash = ?, secret_last4 = ?, secret_rotated_at = ? WHERE vendor_id = ?",
            (_hash_secret(secret), secret[-4:], _now(), vendor_id),
        )
    return secret if cur.rowcount else None


def verify_secret(conn: sqlite3.Connection, vendor_id: str, secret: str) -> Vendor | None:
    """Check a SharedSecret from an incoming cXML request. Returns the vendor if it matches."""
    row = conn.execute("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,)).fetchone()
    expected = row["secret_hash"] if row else _hash_secret("")
    if row is None or not hmac.compare_digest(_hash_secret(secret), expected):
        return None
    return Vendor.from_row(row)
