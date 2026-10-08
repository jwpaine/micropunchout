"""PunchOutOrderMessage (the cart sent back to the procurement system)."""

from dataclasses import dataclass
from decimal import Decimal

from app.cxml.common import (
    USER_AGENT,
    XML_LANG,
    Credential,
    add_credential,
    new_document,
    new_payload_id,
    serialize,
    sub,
)


@dataclass(frozen=True)
class OrderLine:
    """One cart line with its POOM item values already mapped (see app.punchout.poom)."""

    sku: str
    quantity: int
    unit_price: str
    currency: str
    uom: str
    description: str
    classification: str = ""
    classification_domain: str = "UNSPSC"
    supplier_part_aux_id: str = ""
    manufacturer_part_id: str = ""
    manufacturer_name: str = ""
    lead_time: str = ""
    extrinsics: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BuiltOrderMessage:
    payload_id: str
    cxml: bytes
    total: str
    currency: str


class MixedCurrencyError(ValueError):
    pass


def build_order_message(
    *,
    buyer_cookie: str,
    buyer_credentials: list[Credential],
    supplier_credentials: list[Credential],
    lines: list[OrderLine],
    operation_allowed: str = "edit",
) -> BuiltOrderMessage:
    """Build a POOM. The From/To credentials echo the setup request's To/From.

    It carries no SharedSecret: the browser posts it, so the shopper can see it.
    """
    currencies = {line.currency for line in lines}
    if len(currencies) > 1:
        raise MixedCurrencyError(f"Cart mixes currencies: {', '.join(sorted(currencies))}")
    currency = currencies.pop() if currencies else "USD"
    total = sum((Decimal(line.unit_price) * line.quantity for line in lines), Decimal(0))

    payload_id = new_payload_id()
    root = new_document(payload_id)
    header = sub(root, "Header")
    from_ = sub(header, "From")
    for cred in supplier_credentials:
        add_credential(from_, cred)
    to = sub(header, "To")
    for cred in buyer_credentials:
        add_credential(to, cred)
    sender = sub(header, "Sender")
    for cred in supplier_credentials[:1]:
        add_credential(sender, cred)
    sub(sender, "UserAgent", USER_AGENT)

    msg = sub(sub(root, "Message"), "PunchOutOrderMessage")
    sub(msg, "BuyerCookie", buyer_cookie)
    msg_header = sub(msg, "PunchOutOrderMessageHeader", operationAllowed=operation_allowed)
    sub(sub(msg_header, "Total"), "Money", format(total, "f"), currency=currency)

    for line in lines:
        item = sub(msg, "ItemIn", quantity=str(line.quantity))
        item_id = sub(item, "ItemID")
        sub(item_id, "SupplierPartID", line.sku)
        if line.supplier_part_aux_id:
            sub(item_id, "SupplierPartAuxiliaryID", line.supplier_part_aux_id)
        # Element order follows the cXML DTD for ItemDetail.
        detail = sub(item, "ItemDetail")
        sub(sub(detail, "UnitPrice"), "Money", line.unit_price, currency=line.currency)
        sub(detail, "Description", line.description).set(XML_LANG, "en")
        sub(detail, "UnitOfMeasure", line.uom)
        # Classification is required by the DTD even when the product has no code.
        sub(detail, "Classification", line.classification, domain=line.classification_domain or "UNSPSC")
        if line.manufacturer_part_id:
            sub(detail, "ManufacturerPartID", line.manufacturer_part_id)
        if line.manufacturer_name:
            sub(detail, "ManufacturerName", line.manufacturer_name)
        if line.lead_time.isdigit():
            sub(detail, "LeadTime", line.lead_time)
        for name, value in line.extrinsics:
            sub(detail, "Extrinsic", value, name=name)

    return BuiltOrderMessage(payload_id=payload_id, cxml=serialize(root), total=format(total, "f"), currency=currency)
