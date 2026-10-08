from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.config import public_base_url
from app.db import get_db
from app.punchout.service import handle_setup

router = APIRouter()

CXML_MEDIA_TYPE = "text/xml; charset=UTF-8"


@router.post("/cxml/v/{vendor_id}/setup")
async def punchout_setup(vendor_id: str, request: Request, conn=Depends(get_db)):
    body = await request.body()
    result = handle_setup(conn, vendor_id, body, public_base_url(request))
    return Response(result.body, media_type=CXML_MEDIA_TYPE)
