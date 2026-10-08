"""PunchOutSetupRequest parsing and PunchOutSetupResponse building."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from lxml import etree

from app.cxml.common import (
    USER_AGENT,
    Credential,
    CXMLError,
    add_credential,
    credentials,
    new_document,
    new_payload_id,
    parse,
    serialize,
    sub,
    text,
)

OPERATIONS = {"create", "edit", "inspect"}


@dataclass(frozen=True)
class Contact:
    role: str
    name: str
    email: str


@dataclass(frozen=True)
class SetupItem:
    supplier_part_id: str
    supplier_part_aux_id: str
    quantity: Decimal


@dataclass
class SetupRequest:
    payload_id: str
    from_credentials: list[Credential]
    to_credentials: list[Credential]
    sender_credentials: list[Credential]
    shared_secret: str
    user_agent: str
    operation: str
    buyer_cookie: str
    browser_form_post_url: str
    extrinsics: dict[str, str] = field(default_factory=dict)
    contacts: list[Contact] = field(default_factory=list)
    ship_to: dict[str, str] = field(default_factory=dict)
    items: list[SetupItem] = field(default_factory=list)

    def contact(self) -> Contact | None:
        """The shopper's contact: role endUser if present, else the first one."""
        return next((c for c in self.contacts if c.role == "endUser"), self.contacts[0] if self.contacts else None)

    def lookup(self, source: str) -> str:
        """Resolve one mapping source part, e.g. "extrinsic:UserEmail" or "shipto:city" (see app.mapping)."""
        kind, _, key = source.partition(":")
        key = key.strip()
        if kind == "extrinsic":
            return self.extrinsics.get(key, "")
        if kind == "contact" and key in ("name", "email"):
            contact = self.contact()
            return getattr(contact, key) if contact else ""
        if kind == "shipto":
            return self.ship_to.get(key, "")
        if kind == "from" and key in ("domain", "identity") and self.from_credentials:
            return getattr(self.from_credentials[0], key)
        return ""


def parse_setup_request(data: bytes) -> SetupRequest:
    root = parse(data)
    header = root.find("Header")
    req = root.find("Request/PunchOutSetupRequest")
    if header is None or req is None:
        raise CXMLError(400, "Bad Request", "Not a PunchOutSetupRequest")

    sender = header.find("Sender")
    ship_to = req.find("ShipTo/Address")
    operation = req.get("operation", "").strip()
    if operation not in OPERATIONS:
        raise CXMLError(400, "Bad Request", f"Unsupported operation {operation!r}")

    browser_form_post_url = text(req, "BrowserFormPost/URL")
    if not browser_form_post_url.startswith(("https://", "http://")):
        raise CXMLError(400, "Bad Request", "Missing or invalid BrowserFormPost URL")

    items = []
    for item in req.findall("ItemOut"):
        try:
            quantity = Decimal(item.get("quantity", "1"))
        except InvalidOperation:
            raise CXMLError(400, "Bad Request", "Invalid ItemOut quantity") from None
        items.append(
            SetupItem(
                supplier_part_id=text(item, "ItemID/SupplierPartID"),
                supplier_part_aux_id=text(item, "ItemID/SupplierPartAuxiliaryID"),
                quantity=quantity,
            )
        )

    return SetupRequest(
        payload_id=root.get("payloadID", ""),
        from_credentials=credentials(header.find("From")),
        to_credentials=credentials(header.find("To")),
        sender_credentials=credentials(sender),
        shared_secret=text(sender, "Credential/SharedSecret"),
        user_agent=text(sender, "UserAgent"),
        operation=operation,
        buyer_cookie=text(req, "BuyerCookie"),
        browser_form_post_url=browser_form_post_url,
        extrinsics={e.get("name", ""): (e.text or "").strip() for e in req.findall("Extrinsic")},
        contacts=[
            Contact(role=c.get("role", ""), name=text(c, "Name"), email=text(c, "Email"))
            for c in req.findall("Contact")
        ],
        ship_to=parse_ship_to(ship_to),
        items=items,
    )


def parse_ship_to(address: etree._Element | None) -> dict[str, str]:
    if address is None:
        return {}
    postal = address.find("PostalAddress")
    country = postal.find("Country") if postal is not None else None
    return {
        "address_id": address.get("addressID", "").strip(),
        "name": text(address, "Name"),
        "street": ", ".join(t for s in (postal.findall("Street") if postal is not None else []) if (t := text(s))),
        "city": text(postal, "City"),
        "state": text(postal, "State"),
        "postal_code": text(postal, "PostalCode"),
        "country": (country.get("isoCountryCode", "") or text(country)) if country is not None else "",
    }


def build_setup_response(start_page_url: str) -> bytes:
    root = new_document(new_payload_id())
    resp = sub(root, "Response")
    sub(resp, "Status", code="200", text="OK")
    sub(sub(sub(resp, "PunchOutSetupResponse"), "StartPage"), "URL", start_page_url)
    return serialize(root)


def parse_setup_response(data: bytes) -> tuple[int, str, str]:
    """Return (status code, status text or detail, StartPage URL). Used by the admin test tool."""
    root = parse(data)
    status = root.find("Response/Status")
    code = int(status.get("code", "0")) if status is not None else 0
    message = ((status.text or "").strip() or status.get("text", "")) if status is not None else ""
    return code, message, text(root, "Response/PunchOutSetupResponse/StartPage/URL")


def build_setup_request(
    *,
    from_cred: Credential,
    to_cred: Credential,
    sender_cred: Credential,
    shared_secret: str,
    browser_form_post_url: str,
    buyer_cookie: str,
    operation: str = "create",
    extrinsics: dict[str, str] | None = None,
    items: list[tuple[str, int]] = (),
) -> bytes:
    """Build a PunchOutSetupRequest the way a procurement system would. For testing."""
    root = new_document(new_payload_id("buyer.test"))
    header = sub(root, "Header")
    add_credential(sub(header, "From"), from_cred)
    add_credential(sub(header, "To"), to_cred)
    sender = sub(header, "Sender")
    add_credential(sender, sender_cred, shared_secret)
    sub(sender, "UserAgent", USER_AGENT + " test buyer")

    req = sub(sub(root, "Request", deploymentMode="test"), "PunchOutSetupRequest", operation=operation)
    sub(req, "BuyerCookie", buyer_cookie)
    for name, value in (extrinsics or {}).items():
        sub(req, "Extrinsic", value, name=name)
    sub(sub(req, "BrowserFormPost"), "URL", browser_form_post_url)
    sub(sub(req, "SupplierSetup"), "URL", "https://example.invalid/unused")
    for sku, qty in items:
        item = sub(req, "ItemOut", quantity=str(qty))
        sub(sub(item, "ItemID"), "SupplierPartID", sku)
    return serialize(root)
