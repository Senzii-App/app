"""Admin routes — super-user dashboard page and Prometheus metrics.
Direct port of src/routes/admin.rs.

All routes require role='super'.
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse

from app.middleware.auth import require_super_session
from app.db.server_metrics import render_metrics

router = APIRouter()

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "site")


@router.get("/admin")
async def admin_dashboard(request: Request):
    """Super-user admin dashboard (metrics + traffic analytics).

    Requires super role. Redirects to login if not authenticated.
    Mirrors Rust: admin_dashboard — serves static/admin.html on success,
    redirects to /auth/login on auth failure.
    """
    try:
        require_super_session(request)
    except Exception:
        return RedirectResponse("/auth/login", status_code=302)

    admin_html = os.path.join(_STATIC_DIR, "admin.html")
    with open(admin_html, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@router.get("/admin/metrics")
async def get_metrics(request: Request):
    """Prometheus metrics endpoint.

    Requires super role. Returns 401 if not authenticated.
    Mirrors Rust: get_metrics — text/plain Prometheus format on success.
    """
    try:
        require_super_session(request)
    except Exception:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = render_metrics()
    return PlainTextResponse(
        body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )