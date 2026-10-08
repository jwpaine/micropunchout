"""Turn cart lines into POOM items using the punchout's "poom" field mappings."""

from app.cxml.order_message import OrderLine
from app.mapping import map_fields
from app.punchout.sessions import CartLine
from app.punchout_config import POOM_EXTRINSIC_PREFIX, PunchoutConfig


class MappingError(ValueError):
    pass


def order_lines(lines: list[CartLine], config: PunchoutConfig, session_fields: dict[str, str]) -> list[OrderLine]:
    """Map each cart line. Raises MappingError if a required cart field comes out empty."""
    rules = config.poom_rules()
    result = []
    for line in lines:

        def lookup(source: str, line: CartLine = line) -> str:
            kind, _, key = source.partition(":")
            if kind == "product":
                value = getattr(line, key, "")
                return "" if value is None else str(value)
            if kind == "session":
                return session_fields.get(key, "")
            return ""

        values, missing = map_fields(rules, lookup)
        if missing:
            raise MappingError(f"{line.sku}: no value for {', '.join(missing)}")
        result.append(
            OrderLine(
                sku=line.sku,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=line.currency,
                uom=line.uom,
                description=values.get("item.description") or line.name,
                classification=values.get("item.classification", ""),
                classification_domain=values.get("item.classification_domain", ""),
                supplier_part_aux_id=values.get("item.supplier_part_aux_id", ""),
                manufacturer_part_id=values.get("item.manufacturer_part_id", ""),
                manufacturer_name=values.get("item.manufacturer_name", ""),
                lead_time=values.get("item.lead_time", ""),
                extrinsics=tuple(
                    (target[len(POOM_EXTRINSIC_PREFIX):], value)
                    for target, value in values.items()
                    if target.startswith(POOM_EXTRINSIC_PREFIX) and value
                ),
            )
        )
    return result
