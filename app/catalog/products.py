import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

PAGE_SIZE = 50
MAX_PRICE = Decimal("1000000000")
TRUE_VALUES = {"1", "true", "yes", "y"}
FALSE_VALUES = {"0", "false", "no", "n"}


class ProductError(ValueError):
    pass


@dataclass(frozen=True)
class ProductInput:
    """A validated product as it comes from any catalog source (CSV, vendor API, ...)."""

    sku: str
    name: str
    unit_price: str
    currency: str = "USD"
    uom: str = "EA"
    description: str = ""
    unspsc: str = ""
    manufacturer_name: str = ""
    manufacturer_part_id: str = ""
    image_url: str = ""
    lead_time_days: int | None = None
    active: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ProductInput":
        """Validate and normalize loosely typed input. Raises ProductError listing every problem."""
        problems: list[str] = []

        def text(key: str, max_len: int = 200) -> str:
            value = str(raw.get(key) or "").strip()
            if len(value) > max_len:
                problems.append(f"{key} is longer than {max_len} characters")
            return value

        sku = text("sku", 100)
        name = text("name")
        description = text("description", 5000)
        manufacturer_name = text("manufacturer_name")
        manufacturer_part_id = text("manufacturer_part_id", 100)
        currency = text("currency").upper() or "USD"
        uom = text("uom").upper() or "EA"
        unspsc = text("unspsc")
        image_url = text("image_url", 2000)

        if not sku:
            problems.append("sku is required")
        if not name:
            problems.append("name is required")

        unit_price = ""
        try:
            price = Decimal(text("unit_price"))
            if not price.is_finite() or price < 0 or price >= MAX_PRICE:
                raise InvalidOperation
            if price.as_tuple().exponent < -4:
                problems.append("unit_price has more than 4 decimal places")
            unit_price = format(price, "f")  # plain notation: "1E+2" becomes "100"
        except InvalidOperation:
            problems.append("unit_price must be a non-negative number")

        if not re.fullmatch(r"[A-Z]{3}", currency):
            problems.append("currency must be a 3-letter code like USD")
        if not re.fullmatch(r"[A-Z0-9]{1,3}", uom):
            problems.append("uom must be a unit code like EA, BX or CS")
        if unspsc and not re.fullmatch(r"\d{8}", unspsc):
            problems.append("unspsc must be 8 digits")
        if image_url and not re.match(r"https?://", image_url):
            problems.append("image_url must start with http:// or https://")

        lead_time_days = None
        if lead := text("lead_time_days"):
            if lead.isdigit():
                lead_time_days = int(lead)
            else:
                problems.append("lead_time_days must be a whole number")

        active = True
        if flag := text("active").lower():
            if flag in TRUE_VALUES:
                active = True
            elif flag in FALSE_VALUES:
                active = False
            else:
                problems.append("active must be yes/no, true/false or 1/0")

        if problems:
            raise ProductError("; ".join(problems))
        return cls(
            sku=sku,
            name=name,
            unit_price=unit_price,
            currency=currency,
            uom=uom,
            description=description,
            unspsc=unspsc,
            manufacturer_name=manufacturer_name,
            manufacturer_part_id=manufacturer_part_id,
            image_url=image_url,
            lead_time_days=lead_time_days,
            active=active,
        )


INPUT_FIELDS = [f.name for f in fields(ProductInput)]


@dataclass(frozen=True)
class Product(ProductInput):
    id: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Product":
        data = {k: row[k] for k in row.keys() if k != "vendor_pk"}
        data["active"] = bool(data["active"])
        return cls(**data)


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _db_values(p: ProductInput) -> dict[str, object]:
    values = asdict(p)
    values["active"] = int(p.active)
    return values


def sync_products(
    conn: sqlite3.Connection, vendor_pk: int, items: Iterable[ProductInput], deactivate_missing: bool = False
) -> SyncResult:
    """Insert or update products by SKU in one transaction.

    With ``deactivate_missing``, active products not in ``items`` are deactivated
    (a full sync). Products are never deleted, so past carts and orders still resolve.
    """
    result = SyncResult()
    now = _now()
    existing = {
        row["sku"]: row for row in conn.execute("SELECT * FROM products WHERE vendor_pk = ?", (vendor_pk,))
    }
    seen: set[str] = set()
    cols = ", ".join(INPUT_FIELDS)
    placeholders = ", ".join(f":{f}" for f in INPUT_FIELDS)
    assignments = ", ".join(f"{f} = :{f}" for f in INPUT_FIELDS)

    with conn:
        for item in items:
            if item.sku in seen:
                raise ProductError(f"Duplicate SKU {item.sku!r}")
            seen.add(item.sku)
            values = _db_values(item)
            row = existing.get(item.sku)
            if row is None:
                conn.execute(
                    f"INSERT INTO products (vendor_pk, {cols}, created_at, updated_at)"
                    f" VALUES (:vendor_pk, {placeholders}, :now, :now)",
                    {**values, "vendor_pk": vendor_pk, "now": now},
                )
                result.created += 1
            elif any(row[k] != v for k, v in values.items()):
                conn.execute(
                    f"UPDATE products SET {assignments}, updated_at = :now WHERE id = :id",
                    {**values, "now": now, "id": row["id"]},
                )
                result.updated += 1
            else:
                result.unchanged += 1

        if deactivate_missing:
            for sku, row in existing.items():
                if sku not in seen and row["active"]:
                    conn.execute("UPDATE products SET active = 0, updated_at = ? WHERE id = ?", (now, row["id"]))
                    result.deactivated += 1
    return result


def count_products(conn: sqlite3.Connection, vendor_pk: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM products WHERE vendor_pk = ?", (vendor_pk,)).fetchone()[0]


def search_products(
    conn: sqlite3.Connection,
    vendor_pk: int,
    query: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    active_only: bool = False,
) -> tuple[list[Product], int]:
    """Return one page of products matching ``query`` (SKU, name or manufacturer part), and the total match count."""
    where = "vendor_pk = ?"
    params: list[object] = [vendor_pk]
    if active_only:
        where += " AND active = 1"
    if query := query.strip():
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where += (
            " AND (sku LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR manufacturer_part_id LIKE ? ESCAPE '\\')"
        )
        params += [like, like, like]
    total = conn.execute(f"SELECT COUNT(*) FROM products WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM products WHERE {where} ORDER BY sku LIMIT ? OFFSET ?",
        [*params, page_size, (max(page, 1) - 1) * page_size],
    ).fetchall()
    return [Product.from_row(r) for r in rows], total
