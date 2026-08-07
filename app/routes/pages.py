"""Pages router — static HTML page serving and redirects.

Direct port of src/routes/pages.rs from the Rust/Axum project.

Serves only web-app pages (login, dashboards, portals, auth forms, legal
pages, MCP docs). The marketing site — landing page, SEO pages, comparison
pages, lead magnets — lives in the separate Senzii-App/site repo and is
served from Vercel at senzii.com.

All HTML files are read from the static/ directory at runtime via
`open(f'./static/{filename}').read()` and returned as HTMLResponse.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.middleware.auth import (
    require_auth,
    require_staff_session,
    require_client_session,
    require_super_session,
    USER_ID_KEY,
    ROLE_KEY,
)


router = APIRouter(prefix="")


def _serve(filename: str) -> HTMLResponse:
    """Read a static HTML file and return it as HTMLResponse."""
    return HTMLResponse(open(f"./static/{filename}").read())


# ── Core pages ──────────────────────────────────────────────────────────────

@router.get("/")
async def landing(request: Request):
    """GET / — sign-in page.

    app.senzii.com is the application host, not the marketing site (which is
    served from Vercel at senzii.com). Users already logged in are sent
    straight to their dashboard.
    """
    user_id = request.session.get(USER_ID_KEY)
    if user_id is not None:
        role = request.session.get(ROLE_KEY)
        redirect = "/admin" if role == "super" else "/dashboard"
        return RedirectResponse(redirect, status_code=307)
    return _serve("login.html")


@router.get("/favicon.svg")
async def favicon():
    """GET /favicon.svg — serve favicon with long cache headers."""
    content = open("./static/favicon.svg").read()
    return Response(
        content=content,
        media_type="image/svg+xml",
        headers={"cache-control": "public, max-age=31536000, immutable"},
    )


@router.get("/dashboard")
async def dashboard(request: Request):
    """GET /dashboard — admin dashboard (SPA). Requires admin or super session.

    Super users are redirected to /admin.
    Unauthenticated users are redirected to /auth/login.
    """
    role = request.session.get("role")
    if role == "super":
        return RedirectResponse("/admin", status_code=307)
    try:
        require_auth(request)
    except Exception:
        return RedirectResponse("/auth/login", status_code=307)
    return _serve("dashboard.html")


@router.get("/terms")
async def terms_of_service():
    """GET /terms — Terms of Service page."""
    return _serve("terms-of-service.html")


@router.get("/privacy")
async def privacy_policy():
    """GET /privacy — Privacy Policy page."""
    return _serve("privacy-policy.html")


@router.get("/refunds")
async def refund_policy():
    """GET /refunds — Refund Policy page."""
    return _serve("refund-policy.html")


@router.get("/welcome")
async def welcome():
    """GET /welcome — welcome page."""
    return _serve("welcome.html")


# ── Staff portal ────────────────────────────────────────────────────────────

@router.get("/staff")
async def staff_portal(request: Request):
    """GET /staff — staff portal SPA. Requires staff session."""
    try:
        require_staff_session(request)
    except Exception:
        return RedirectResponse("/staff/login", status_code=307)
    return _serve("staff.html")


@router.get("/staff/login")
async def staff_login_redirect(request: Request):
    """GET /staff/login — Staff login page.

    We intentionally do NOT verify or consume the magic link token here.
    The page's client-side JavaScript calls /staff/api/login?token=... to
    verify and consume the token. This prevents messaging-app link-preview
    bots (iMessage, Android Messages, etc.) from consuming the token via
    their pre-fetch HTTP GET before the human taps the link.
    """
    # If already logged in as staff, go straight to the portal
    try:
        require_staff_session(request)
        return RedirectResponse("/staff", status_code=307)
    except Exception:
        pass
    return _serve("staff-login.html")


@router.get("/staff/set-password")
async def get_staff_set_password():
    """GET /staff/set-password — Show staff set-password page."""
    return _serve("staff-set-password.html")


# ── Client portal ────────────────────────────────────────────────────────────

@router.get("/client")
async def client_portal(request: Request):
    """GET /client — client portal SPA. Requires client session."""
    try:
        require_client_session(request)
    except Exception:
        return RedirectResponse("/client/login", status_code=307)
    return _serve("client.html")


@router.get("/client/login")
async def client_login_redirect(request: Request):
    """GET /client/login — Client login page.

    We intentionally do NOT verify or consume the magic link token here.
    The page's client-side JavaScript calls /client/api/login?token=... to
    verify and consume the token. This prevents messaging-app link-preview
    bots (iMessage, Android Messages, etc.) from consuming the token via
    their pre-fetch HTTP GET before the human taps the link.
    """
    # If already logged in as client, go straight to the portal
    try:
        require_client_session(request)
        return RedirectResponse("/client", status_code=307)
    except Exception:
        pass
    return _serve("client-login.html")


@router.get("/client/set-password")
async def get_client_set_password():
    """GET /client/set-password — Show client set-password page."""
    return _serve("client-set-password.html")


# ── Admin leads dashboard ────────────────────────────────────────────────────

@router.get("/leads-dashboard")
async def leads_dashboard(request: Request):
    """GET /leads-dashboard — Admin view: captured lead data table (super-only)."""
    try:
        require_super_session(request)
    except Exception:
        return RedirectResponse("/auth/login", status_code=307)
    return _serve("leads-dashboard.html")


# ── MCP / integrations ───────────────────────────────────────────────────────

@router.get("/integrations")
async def integrations():
    """GET /integrations — MCP server landing page (Senzii MCP integration for Claude, ChatGPT, etc.)."""
    return _serve("integrations.html")


@router.get("/ai-integration")
async def mcp_docs():
    """GET /ai-integration — MCP server developer docs (tool reference, OAuth endpoints, setup guide).

    Rust route path is /mcp; mapped to /ai-integration per task spec.
    Serves static/mcp.html.
    """
    return _serve("mcp.html")


@router.get("/mcp")
async def mcp_docs_alias():
    """GET /mcp — alias for /ai-integration (matches Rust route)."""
    return _serve("mcp.html")


@router.get("/thank-you")
async def thank_you():
    """GET /thank-you — Thank-you / confirmation page shown after lead capture."""
    return _serve("thank-you.html")


# ── Auth set-password (admin) ─────────────────────────────────────────────────
# Rust serves /auth/set-password from this router; the Python auth router is
# mounted at /auth prefix, so we expose it here as /auth/set-password to keep
# all page-serving logic in one place (matches Rust layout).

@router.get("/auth/set-password")
async def get_set_password_form():
    """GET /auth/set-password — Show set-password form (query params carry userId/userEmail from magic link)."""
    return _serve("auth-set-password.html")
