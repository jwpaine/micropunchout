import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.cxml.common import Credential

START_TOKEN_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(hours=8)
MAX_QUANTITY = 1_000_000


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@dataclass(frozen=True)
class PunchoutSession:
    id: int
    vendor_pk: int
    connection_pk: int
    shop_token: str
    status: str
    operation: str
    buyer_cookie: str
    browser_form_post_url: str
    user_external_id: str
    user_email: str
    user_name: str
    ship_to_id: str
    payload_id: str
    raw_request: str
    from_credentials: list[Credential]
    to_credentials: list[Credential]
    created_at: str
    expires_at: str
    started_at: str | None
    returned_at: str | None
    shopper_pk: int | None = None
    # Mapped setup request fields (punchout_config "posr"), e.g. {"user.email": ..., "cost_center": ...}.
    fields: dict = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.operation == "inspect"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PunchoutSession":
        return cls(
            id=row["id"],
            vendor_pk=row["vendor_pk"],
            connection_pk=row["connection_pk"],
            shop_token=row["shop_token"],
            status=row["status"],
            operation=row["operation"],
            buyer_cookie=row["buyer_cookie"],
            browser_form_post_url=row["browser_form_post_url"],
            user_external_id=row["user_external_id"],
            user_email=row["user_email"],
            user_name=row["user_name"],
            ship_to_id=row["ship_to_id"],
            payload_id=row["payload_id"],
            raw_request=row["raw_request"],
            from_credentials=[Credential(**c) for c in json.loads(row["from_credential"])],
            to_credentials=[Credential(**c) for c in json.loads(row["to_credential"])],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            started_at=row["started_at"],
            returned_at=row["returned_at"],
            shopper_pk=row["shopper_pk"],
            fields=json.loads(row["fields"]),
        )


@dataclass(frozen=True)
class CartLine:
    product_pk: int
    quantity: int
    sku: str
    name: str
    description: str
    unit_price: str
    currency: str
    uom: str
    unspsc: str
    manufacturer_name: str
    manufacturer_part_id: str
    lead_time_days: int | None
    image_url: str
    active: bool

    @property
    def line_total(self) -> str:
        return format(Decimal(self.unit_price) * self.quantity, "f")


def create_session(
    conn: sqlite3.Connection,
    *,
    vendor_pk: int,
    connection_pk: int,
    operation: str,
    buyer_cookie: str,
    browser_form_post_url: str,
    user: dict[str, str],
    ship_to_id: str,
    payload_id: str,
    raw_request: str,
    from_credentials: list[Credential],
    to_credentials: list[Credential],
    fields: dict[str, str],
    shopper_pk: int,
) -> tuple[PunchoutSession, str]:
    """Create a pending session. Returns it with the one-time start token for the StartPage URL."""
    now = _now()
    start_token = secrets.token_urlsafe(32)
    with conn:
        cur = conn.execute(
            """INSERT INTO punchout_sessions (
                vendor_pk, connection_pk, start_token, shop_token, status, operation, buyer_cookie,
                browser_form_post_url, user_external_id, user_email, user_name, ship_to_id, payload_id,
                raw_request, from_credential, to_credential, created_at, start_expires_at, expires_at,
                fields, shopper_pk
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vendor_pk,
                connection_pk,
                start_token,
                secrets.token_urlsafe(32),
                operation,
                buyer_cookie,
                browser_form_post_url,
                user.get("external_id", ""),
                user.get("email", ""),
                user.get("name", ""),
                ship_to_id,
                payload_id,
                raw_request,
                json.dumps([c.to_dict() for c in from_credentials]),
                json.dumps([c.to_dict() for c in to_credentials]),
                _iso(now),
                _iso(now + START_TOKEN_TTL),
                _iso(now + SESSION_TTL),
                json.dumps(fields, sort_keys=True),
                shopper_pk,
            ),
        )
    return get_session(conn, cur.lastrowid), start_token


def get_session(conn: sqlite3.Connection, session_pk: int) -> PunchoutSession | None:
    row = conn.execute("SELECT * FROM punchout_sessions WHERE id = ?", (session_pk,)).fetchone()
    return PunchoutSession.from_row(row) if row else None


def start_session(conn: sqlite3.Connection, start_token: str) -> PunchoutSession | None:
    """Use a start token. Each token works once, and only before it expires."""
    now = _iso(_now())
    with conn:
        row = conn.execute(
            "UPDATE punchout_sessions SET status = 'active', started_at = ?"
            " WHERE start_token = ? AND status = 'pending' AND start_expires_at > ?"
            " RETURNING *",
            (now, start_token, now),
        ).fetchone()
    return PunchoutSession.from_row(row) if row else None


def get_shop_session(conn: sqlite3.Connection, shop_token: str) -> PunchoutSession | None:
    row = conn.execute("SELECT * FROM punchout_sessions WHERE shop_token = ?", (shop_token,)).fetchone()
    return PunchoutSession.from_row(row) if row else None


def is_expired(session: PunchoutSession) -> bool:
    return session.expires_at <= _iso(_now())


def recent_sessions(conn: sqlite3.Connection, vendor_pk: int, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.id, s.status, s.operation, s.user_name, s.user_email, s.created_at, c.name AS connection_name,
                  (SELECT COUNT(*) FROM order_messages m WHERE m.session_pk = s.id) AS order_message_count
           FROM punchout_sessions s JOIN connections c ON c.id = s.connection_pk
           WHERE s.vendor_pk = ? ORDER BY s.id DESC LIMIT ?""",
        (vendor_pk, limit),
    ).fetchall()


# --- cart -------------------------------------------------------------------


def _product_pk(conn: sqlite3.Connection, vendor_pk: int, product_pk: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM products WHERE id = ? AND vendor_pk = ? AND active = 1", (product_pk, vendor_pk)
    ).fetchone()
    return row["id"] if row else None


def add_to_cart(conn: sqlite3.Connection, session: PunchoutSession, product_pk: int, quantity: int) -> bool:
    if quantity < 1 or _product_pk(conn, session.vendor_pk, product_pk) is None:
        return False
    with conn:
        conn.execute(
            "INSERT INTO cart_lines (session_pk, product_pk, quantity) VALUES (?, ?, ?)"
            " ON CONFLICT (session_pk, product_pk) DO UPDATE SET quantity = MIN(quantity + excluded.quantity, ?)",
            (session.id, product_pk, min(quantity, MAX_QUANTITY), MAX_QUANTITY),
        )
    return True


def set_quantity(conn: sqlite3.Connection, session: PunchoutSession, product_pk: int, quantity: int) -> None:
    with conn:
        if quantity <= 0:
            conn.execute("DELETE FROM cart_lines WHERE session_pk = ? AND product_pk = ?", (session.id, product_pk))
        else:
            conn.execute(
                "UPDATE cart_lines SET quantity = ? WHERE session_pk = ? AND product_pk = ?",
                (min(quantity, MAX_QUANTITY), session.id, product_pk),
            )


def prefill_cart(conn: sqlite3.Connection, session: PunchoutSession, items: list[tuple[str, int]]) -> list[str]:
    """Load the items from an edit/inspect setup request into the cart. Returns SKUs that couldn't be added."""
    skipped = []
    for sku, quantity in items:
        row = conn.execute(
            "SELECT id FROM products WHERE vendor_pk = ? AND sku = ? AND active = 1", (session.vendor_pk, sku)
        ).fetchone()
        if row is None or not add_to_cart(conn, session, row["id"], quantity):
            skipped.append(sku)
    return skipped


def cart_lines(conn: sqlite3.Connection, session: PunchoutSession) -> list[CartLine]:
    rows = conn.execute(
        """SELECT l.product_pk, l.quantity, p.sku, p.name, p.description, p.unit_price, p.currency, p.uom, p.unspsc,
                  p.manufacturer_name, p.manufacturer_part_id, p.lead_time_days, p.image_url, p.active
           FROM cart_lines l JOIN products p ON p.id = l.product_pk
           WHERE l.session_pk = ? ORDER BY l.id""",
        (session.id,),
    ).fetchall()
    return [CartLine(**{k: r[k] for k in r.keys()} | {"active": bool(r["active"])}) for r in rows]


def cart_count(conn: sqlite3.Connection, session: PunchoutSession) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM cart_lines WHERE session_pk = ?", (session.id,)
    ).fetchone()[0]


# --- return -----------------------------------------------------------------


def complete_session(
    conn: sqlite3.Connection, session: PunchoutSession, *, payload_id: str, cxml: str, total: str, currency: str,
    line_count: int,
) -> bool:
    """Store the POOM and mark the session returned. False if it was already returned (double submit)."""
    now = _iso(_now())
    with conn:
        cur = conn.execute(
            "UPDATE punchout_sessions SET status = 'returned', returned_at = ? WHERE id = ? AND status = 'active'",
            (now, session.id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO order_messages (session_pk, payload_id, total, currency, line_count, cxml, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session.id, payload_id, total, currency, line_count, cxml, now),
        )
    return True


def order_messages(conn: sqlite3.Connection, session_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM order_messages WHERE session_pk = ? ORDER BY id", (session_pk,)
    ).fetchall()
