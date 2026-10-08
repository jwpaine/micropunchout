"""Where product data comes from.

Every source yields validated ``ProductInput`` items and goes through the same
``sync_products``. CSV upload is the first source; a vendor API source would
implement ``CatalogSource`` and run on a schedule or webhook.
"""

import csv
import io
from dataclasses import dataclass, field
from typing import Protocol

from app.catalog.products import INPUT_FIELDS, ProductError, ProductInput

REQUIRED_COLUMNS = ["sku", "name", "unit_price"]
MAX_ERRORS = 50

CSV_TEMPLATE_ROWS = [
    {
        "sku": "WID-100",
        "name": "Blue widget",
        "unit_price": "12.50",
        "currency": "USD",
        "uom": "EA",
        "description": "Standard blue widget, 10 cm",
        "unspsc": "31160000",
        "manufacturer_name": "Widget Co",
        "manufacturer_part_id": "WC-100-B",
        "image_url": "https://example.com/images/wid-100.jpg",
        "lead_time_days": "3",
        "active": "yes",
    }
]


@dataclass
class ParseResult:
    items: list[ProductInput] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class CatalogSource(Protocol):
    def fetch(self) -> ParseResult: ...


class CsvSource:
    def __init__(self, data: bytes):
        self.data = data

    def fetch(self) -> ParseResult:
        result = ParseResult()
        try:
            text = self.data.decode("utf-8-sig")  # Excel adds a BOM
        except UnicodeDecodeError:
            result.errors.append("File is not UTF-8 text. In Excel, save as \"CSV UTF-8\".")
            return result

        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = [(h or "").strip().lower() for h in reader.fieldnames or []]
        if not headers:
            result.errors.append("File is empty.")
            return result
        reader.fieldnames = headers
        if missing := [c for c in REQUIRED_COLUMNS if c not in headers]:
            result.errors.append(f"Missing required column(s): {', '.join(missing)}")
            return result
        result.ignored_columns = [h for h in headers if h not in INPUT_FIELDS]

        seen: dict[str, int] = {}
        for row in reader:
            line = reader.line_num
            if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                continue  # blank line
            if None in row:
                result.errors.append(f"Line {line}: more values than columns")
                continue
            try:
                item = ProductInput.from_mapping(row)
            except ProductError as e:
                result.errors.append(f"Line {line}: {e}")
            else:
                if item.sku in seen:
                    result.errors.append(f"Line {line}: duplicate SKU {item.sku!r} (first on line {seen[item.sku]})")
                else:
                    seen[item.sku] = line
                    result.items.append(item)
            if len(result.errors) >= MAX_ERRORS:
                result.errors.append(f"Stopped after {MAX_ERRORS} errors.")
                break

        if not result.items and not result.errors:
            result.errors.append("File has no product rows.")
        return result


def csv_template() -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=INPUT_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(CSV_TEMPLATE_ROWS)
    return out.getvalue()
