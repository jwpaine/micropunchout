import logging
import sqlite3
from dataclasses import dataclass

from app import connections, punchout_config, vendors
from app.cxml.common import CXMLError, redact_secrets, status_response
from app.cxml.setup import build_setup_response, parse_setup_request
from app.mapping import map_fields
from app.punchout import sessions, shoppers

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SetupResult:
    body: bytes
    session_pk: int | None = None


def handle_setup(conn: sqlite3.Connection, vendor_id: str, body: bytes, base_url: str) -> SetupResult:
    """Handle a PunchOutSetupRequest and return the cXML response.

    Problems are reported as a cXML <Status> (with HTTP 200), which is what procurement systems expect.
    """
    try:
        req = parse_setup_request(body)

        # The vendor in the URL plus its shared secret authenticate the caller. Unknown vendors get the same
        # answer as a wrong secret, so vendor IDs can't be probed.
        vendor = vendors.verify_secret(conn, vendor_id, req.shared_secret)
        if vendor is None:
            raise CXMLError(401, "Unauthorized", "Invalid credentials")

        connection = connections.find_by_credentials(conn, vendor.id, req.from_credentials)
        if connection is None:
            creds = ", ".join(f"{c.domain}:{c.identity}" for c in req.from_credentials) or "none"
            raise CXMLError(403, "Forbidden", f"No active buyer connection for From credential ({creds})")

        settings = punchout_config.effective(vendor.config, connection.config)
        fields, missing = map_fields(settings.posr_rules(), req.lookup)
        if missing:
            raise CXMLError(400, "Bad Request", f"Missing required field(s): {', '.join(missing)}")
        shopper_pk = shoppers.upsert(conn, vendor.id, connection.id, fields)

        session, start_token = sessions.create_session(
            conn,
            vendor_pk=vendor.id,
            connection_pk=connection.id,
            operation=req.operation,
            buyer_cookie=req.buyer_cookie,
            browser_form_post_url=req.browser_form_post_url,
            user={
                "external_id": fields["user.external_id"],
                "email": fields.get("user.email", ""),
                "name": fields.get("user.name", ""),
            },
            ship_to_id=fields.get("ship_to.id", ""),
            payload_id=req.payload_id,
            # Stored for debugging, with the shared secret replaced by ****.
            raw_request=redact_secrets(body.decode("utf-8", errors="replace")),
            from_credentials=req.from_credentials,
            to_credentials=req.to_credentials,
            fields=fields,
            shopper_pk=shopper_pk,
        )
        if req.items:
            items = [
                (i.supplier_part_id, int(i.quantity))
                for i in req.items
                if i.quantity == i.quantity.to_integral_value() and i.quantity > 0
            ]
            if skipped := sessions.prefill_cart(conn, session, items):
                log.info("Session %s: could not restore SKUs %s", session.id, skipped)

        start_page = f"{(settings.punchout.base_url or base_url).rstrip('/')}/punchout/start/{start_token}"
        return SetupResult(build_setup_response(start_page), session.id)

    except CXMLError as e:
        log.info("Setup request for vendor %s rejected: %s %s", vendor_id, e.code, e.detail or e.text)
        return SetupResult(status_response(e.code, e.text, e.detail))
