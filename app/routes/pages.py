from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import User, require_user
from app.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "home.html", {"user": user})
