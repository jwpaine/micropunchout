import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from lxml import etree

CXML_VERSION = "1.2.014"
DOCTYPE = f'<!DOCTYPE cXML SYSTEM "http://xml.cxml.org/schemas/cXML/{CXML_VERSION}/cXML.dtd">'
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
USER_AGENT = "Punchout 0.1"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024

# Incoming cXML is untrusted: never load DTDs, fetch from the network or expand entities (XXE).
_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False
)


class CXMLError(Exception):
    """A problem to report back to the sender as a cXML <Status>."""

    def __init__(self, code: int, text: str, detail: str = ""):
        super().__init__(detail or text)
        self.code = code
        self.text = text
        self.detail = detail


@dataclass(frozen=True)
class Credential:
    domain: str
    identity: str

    def to_dict(self) -> dict[str, str]:
        return {"domain": self.domain, "identity": self.identity}


def parse(data: bytes) -> etree._Element:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise CXMLError(413, "Request Entity Too Large")
    try:
        root = etree.fromstring(data, _PARSER)
    except etree.XMLSyntaxError as e:
        raise CXMLError(400, "Bad Request", f"Malformed XML: {e}") from None
    if root.tag != "cXML":
        raise CXMLError(400, "Bad Request", f"Root element is <{root.tag}>, expected <cXML>")
    return root


def text(el: etree._Element | None, path: str = ".") -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None else ""


def credentials(parent: etree._Element | None) -> list[Credential]:
    if parent is None:
        return []
    return [
        Credential(domain=c.get("domain", "").strip(), identity=text(c, "Identity"))
        for c in parent.findall("Credential")
    ]


def new_payload_id(host: str = "punchout") -> str:
    return f"{int(time.time() * 1000)}.{secrets.token_hex(8)}@{host}"


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_document(payload_id: str) -> etree._Element:
    root = etree.Element("cXML", payloadID=payload_id, timestamp=timestamp())
    root.set(XML_LANG, "en-US")
    return root


def sub(parent: etree._Element, tag: str, text_: str | None = None, **attrs: str) -> etree._Element:
    el = etree.SubElement(parent, tag, **attrs)
    if text_ is not None:
        el.text = text_
    return el


def add_credential(parent: etree._Element, cred: Credential, shared_secret: str | None = None) -> None:
    c = sub(parent, "Credential", domain=cred.domain)
    sub(c, "Identity", cred.identity)
    if shared_secret is not None:
        sub(c, "SharedSecret", shared_secret)


def serialize(root: etree._Element, pretty: bool = False) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", doctype=DOCTYPE, pretty_print=pretty)


def status_response(code: int, text_: str, detail: str = "") -> bytes:
    root = new_document(new_payload_id())
    sub(sub(root, "Response"), "Status", detail or None, code=str(code), text=text_)
    return serialize(root)


# Matches the value of every <SharedSecret> element, including CDATA and whitespace.
_SHARED_SECRET = re.compile(r"(<SharedSecret\b[^>]*>)(.*?)(</SharedSecret>)", re.DOTALL)
REDACTED = "****"


def redact_secrets(text: str) -> str:
    """Replace shared secret values with ****, leaving the rest of the document exactly as received."""
    return _SHARED_SECRET.sub(rf"\g<1>{REDACTED}\g<3>", text)


def pretty(data: bytes | str) -> str:
    """Pretty-print a stored cXML document for display; returns the input unchanged if it doesn't parse."""
    raw = data.encode() if isinstance(data, str) else data
    try:
        root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=True))
    except etree.XMLSyntaxError:
        return raw.decode(errors="replace")
    return etree.tostring(root, pretty_print=True, encoding="unicode")
