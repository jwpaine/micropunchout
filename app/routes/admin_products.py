from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, Response

from app.auth import User
from app.auth.dependencies import require_admin
from app.catalog import products, sources
from app.db import get_db
from app.routes.admin import vendor_or_404
from app.templating import templates

router = APIRouter(prefix="/admin/vendors/{vendor_id}/products")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.get("", response_class=HTMLResponse)
def product_list(
    vendor_id: str,
    request: Request,
    q: str = "",
    page: int = 1,
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    vendor = vendor_or_404(conn, vendor_id)
    page = max(page, 1)
    items, total = products.search_products(conn, vendor.id, q, page)
    pages = max(1, -(-total // products.PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "admin/product_list.html",
        {"user": user, "vendor": vendor, "products": items, "total": total, "q": q, "page": page, "pages": pages},
    )


@router.get("/import", response_class=HTMLResponse)
def product_import_form(vendor_id: str, request: Request, user: User = Depends(require_admin), conn=Depends(get_db)):
    vendor = vendor_or_404(conn, vendor_id)
    return templates.TemplateResponse(
        request, "admin/product_import.html", {"user": user, "vendor": vendor, "parsed": None, "result": None}
    )


@router.post("/import", response_class=HTMLResponse)
async def product_import(
    vendor_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    deactivate_missing: Annotated[bool, Form()] = False,
    user: User = Depends(require_admin),
    conn=Depends(get_db),
):
    vendor = vendor_or_404(conn, vendor_id)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    ctx = {"user": user, "vendor": vendor, "result": None, "deactivate_missing": deactivate_missing}

    if len(data) > MAX_UPLOAD_BYTES:
        parsed = sources.ParseResult(errors=[f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."])
    else:
        parsed = sources.CsvSource(data).fetch()
    ctx["parsed"] = parsed

    # All or nothing: if any row is invalid, nothing is imported.
    if not parsed.ok:
        return templates.TemplateResponse(
            request, "admin/product_import.html", ctx, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    ctx["result"] = products.sync_products(conn, vendor.id, parsed.items, deactivate_missing=deactivate_missing)
    return templates.TemplateResponse(request, "admin/product_import.html", ctx)


@router.get("/template.csv")
def product_csv_template(vendor_id: str, _: User = Depends(require_admin)):
    return Response(
        sources.csv_template(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="products-template.csv"'},
    )
