"""Auth routes — Admin auth: login, logout, onboarding, magic link, set-password,
forgot-password, reset-password.

Direct port of src/routes/auth.rs from the Rust/Axum project.

Session-based auth via starlette SessionMiddleware (request.session dict).
Rate-limited: 10 req/60s per IP (in-memory sliding window).
Static HTML files are read from ./static/ at runtime (the include_str! equivalent).
"""
import asyncio
import os
from urllib.parse import quote

import bcrypt
from fastapi import APIRouter, Request, Query, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.db.pool import get_pool
from app.db import password_resets
from app.middleware.auth import (
    save_session,
    destroy_session,
    USER_ID_KEY,
    ROLE_KEY,
)
from app.middleware.rate_limit import RateLimiter, get_client_ip
from app.services.email import send_password_reset_email


router = APIRouter(prefix="")


# ── Static HTML helper (include_str! equivalent) ──────────────────────────────

def _serve(filename: str) -> HTMLResponse:
    """Read a static HTML file and return it as HTMLResponse."""
    return HTMLResponse(open(f"./static/{filename}").read())


# ── Rate limiter (10 req / 60s per IP) ─────────────────────────────────────────

_auth_limiter = RateLimiter(max_requests=10, window_secs=60)


async def rate_limit_dependency(request: Request):
    """Rate limit all auth routes: 10 requests per 60 seconds per client IP.

    Applied as a router-level dependency so it runs for every route below.
    Returns a 429 Response (with Retry-After header) when the limit is exceeded;
    otherwise returns None and the request proceeds normally.
    """
    ip = get_client_ip(request)
    retry_after = _auth_limiter.check(ip)
    if retry_after is not None:
        return Response(
            content="Too many requests. Please try again later.",
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    return None


# Apply the rate-limit dependency to every route in this router.
router.dependencies = [Depends(rate_limit_dependency)]


# ── Request / Response payloads ───────────────────────────────────────────────

class LoginPayload(BaseModel):
    email: str
    password: str


class SetPasswordPayload(BaseModel):
    password: str
    confirm: str
    user_id: int | None = None
    user_email: str | None = None


class ForgotPasswordPayload(BaseModel):
    email: str


class ResetPasswordPayload(BaseModel):
    token: str
    password: str
    confirm_password: str  # JS sends confirmPassword


# ── Handlers ──────────────────────────────────────────────────────────────────

# GET /login — If already logged in, redirect to dashboard (or admin for super
# users); else show login page.
@router.get("/login")
async def get_login(request: Request):
    user_id = request.session.get(USER_ID_KEY)
    role = request.session.get(ROLE_KEY)
    if user_id is not None:
        redirect = "/admin" if role == "super" else "/dashboard"
        return RedirectResponse(redirect, status_code=307)
    return _serve("auth-login.html")


# POST /login — Authenticate admin via email + password.
@router.post("/login")
async def post_login(request: Request, payload: LoginPayload):
    email = payload.email.strip().lower()
    password = payload.password

    if not email or not password:
        return JSONResponse(
            {"error": "Email and password are required."},
            status_code=400,
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT id, email, password_hash, role, organization_id "
                "FROM users WHERE email = $1",
                email,
            )
        except Exception:
            return JSONResponse({"error": "Database error"}, status_code=500)

        if row is None:
            return JSONResponse(
                {"error": "Invalid email or password."},
                status_code=401,
            )

        role: str = row["role"]
        if role not in ("admin", "super"):
            return JSONResponse(
                {"error": "Invalid email or password."},
                status_code=401,
            )

        password_hash = row["password_hash"]
        if not password_hash:
            return JSONResponse(
                {"error": "Invalid email or password."},
                status_code=401,
            )

        try:
            if not bcrypt.checkpw(password.encode(), password_hash.encode()):
                return JSONResponse(
                    {"error": "Invalid email or password."},
                    status_code=401,
                )
        except Exception:
            return JSONResponse(
                {"error": "Invalid email or password."},
                status_code=401,
            )

        uid: int = row["id"]
        uemail: str = row["email"]
        org_id = row["organization_id"] or 0

        save_session(request, uid, uemail, role, org_id)

        redirect = "/admin" if role == "super" else "/dashboard"
        return JSONResponse({"ok": True, "redirect": redirect})


# GET /logout — Destroy session, redirect to login.
@router.get("/logout")
async def get_logout(request: Request):
    destroy_session(request)
    return RedirectResponse("/auth/login", status_code=307)


# GET /onboarding — Post-checkout success page.
@router.get("/onboarding")
async def get_onboarding():
    return _serve("auth-onboarding.html")


# GET /admin-login?token=... — Magic link login for Stripe checkout.
@router.get("/admin-login")
async def get_admin_login(request: Request, token: str | None = Query(default=None)):
    if not token:
        return RedirectResponse("/auth/login", status_code=307)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            record = await conn.fetchrow(
                "SELECT amt.user_id, u.email AS user_email, amt.organization_id, u.role "
                "FROM admin_magic_tokens amt "
                "JOIN users u ON u.id = amt.user_id "
                "WHERE amt.token = $1 AND amt.expires_at > NOW()",
                token,
            )
        except Exception:
            return HTMLResponse("<p>Database error</p>", status_code=500)

        if record is None:
            return HTMLResponse(
                "<p>This magic link is invalid or has expired. "
                "Please request a new one.</p>"
            )

        user_id: int = record["user_id"]
        user_email: str = record["user_email"]
        user_role: str = record["role"]
        org_id = record["organization_id"] or 0

        # Consume token
        await conn.execute(
            "DELETE FROM admin_magic_tokens WHERE token = $1",
            token,
        )

        save_session(request, user_id, user_email, user_role, org_id)

        # Super users go straight to /admin
        if user_role == "super":
            return RedirectResponse("/admin", status_code=307)

        # Check if user already has a password set
        existing = await conn.fetchval(
            "SELECT password_hash FROM users "
            "WHERE id = $1 AND password_hash IS NOT NULL AND password_hash != ''",
            user_id,
        )
        if existing:
            return RedirectResponse("/dashboard", status_code=307)

        # Redirect to set-password page with userId/userEmail as query params
        encoded_email = quote(user_email, safe="")
        return RedirectResponse(
            f"/auth/set-password?userId={user_id}&userEmail={encoded_email}",
            status_code=307,
        )


# GET /set-password — Show set-password form (query params carry
# userId/userEmail from magic link).
@router.get("/set-password")
async def get_set_password_form():
    return _serve("auth-set-password.html")


# POST /set-password — Set password after magic link login.
@router.post("/set-password")
async def post_set_password(request: Request, payload: SetPasswordPayload):
    password = payload.password
    confirm = payload.confirm
    user_id = payload.user_id
    _user_email = payload.user_email  # present from magic link, not used server-side

    if len(password) < 8:
        return JSONResponse(
            {"error": "Password must be at least 8 characters."},
            status_code=400,
        )
    if password != confirm:
        return JSONResponse(
            {"error": "Passwords do not match."},
            status_code=400,
        )

    if user_id is None:
        return RedirectResponse("/auth/login", status_code=307)

    pool = await get_pool()
    async with pool.acquire() as conn:
        hash_bytes = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        hash_str = hash_bytes.decode()

        try:
            row = await conn.fetchrow(
                "UPDATE users SET password_hash = $1 WHERE id = $2 "
                "RETURNING id, email, role, organization_id",
                hash_str,
                user_id,
            )
        except Exception:
            return JSONResponse(
                {"error": "Failed to set password"},
                status_code=500,
            )

        if row is None:
            return JSONResponse(
                {"error": "Failed to set password"},
                status_code=500,
            )

        email: str = row["email"]
        role: str = row["role"]
        org_id = row["organization_id"] or 0
        save_session(request, user_id, email, role, org_id)
        redirect = "/admin" if role == "super" else "/dashboard"
        return JSONResponse({"ok": True, "redirect": redirect})


# ── Password Reset ───────────────────────────────────────────────────────────

# GET /forgot-password — Show the "forgot password" form.
@router.get("/forgot-password")
async def get_forgot_password(request: Request):
    # If already logged in, redirect away
    user_id = request.session.get(USER_ID_KEY)
    if user_id is not None:
        return RedirectResponse("/dashboard", status_code=307)
    return _serve("forgot-password.html")


# POST /forgot-password — Look up email, create reset token, send email.
# Always returns success to prevent email enumeration.
@router.post("/forgot-password")
async def post_forgot_password(payload: ForgotPasswordPayload):
    email = payload.email.strip().lower()

    if not email:
        return JSONResponse({"error": "Email is required."}, status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            user = await conn.fetchrow(
                "SELECT id, email, role FROM users "
                "WHERE email = $1 AND (role = 'admin' OR role = 'super')",
                email,
            )
        except Exception:
            return JSONResponse({"error": "Database error"}, status_code=500)

        if user is not None:
            user_id: int = user["id"]
            user_email: str = user["email"]

            # Create reset token
            try:
                token = await password_resets.create_password_reset_token(
                    conn, user_id
                )
                base_url = os.getenv("BASE_URL", "https://senzii.com")
                reset_link = f"{base_url}/auth/reset-password?token={token}"

                # Send email (fire and forget — don't block the response)
                asyncio.create_task(
                    send_password_reset_email(user_email, reset_link)
                )
            except Exception as e:
                print(f"Failed to create password reset token: {e}")

    # Always return success — don't reveal whether the email exists
    return JSONResponse({
        "ok": True,
        "message": "If an account with that email exists, a reset link has been sent.",
    })


# GET /reset-password?token=... — Show the reset password form.
@router.get("/reset-password")
async def get_reset_password(token: str | None = Query(default=None)):
    if not token:
        return HTMLResponse("<p>Invalid or missing reset token.</p>")

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            verified = await password_resets.verify_password_reset_token(conn, token)
        except Exception:
            return HTMLResponse("<p>Database error</p>", status_code=500)

        if verified is None:
            return HTMLResponse(
                "<p>This password reset link is invalid or has expired. "
                "Please request a new one.</p>"
            )

        # Serve the reset-password HTML page with token and email embedded
        html = open("./static/reset-password.html").read()
        html = html.replace("{{TOKEN}}", token)
        html = html.replace("{{EMAIL}}", verified.user_email)
        return HTMLResponse(html)


# POST /reset-password — Set new password using reset token.
@router.post("/reset-password")
async def post_reset_password(request: Request, payload: ResetPasswordPayload):
    token = payload.token
    password = payload.password
    confirm_password = payload.confirm_password

    if len(password) < 8:
        return JSONResponse(
            {"error": "Password must be at least 8 characters."},
            status_code=400,
        )
    if password != confirm_password:
        return JSONResponse(
            {"error": "Passwords do not match."},
            status_code=400,
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify the token is valid (not expired, not used)
        verified = await password_resets.verify_password_reset_token(conn, token)
        if verified is None:
            return JSONResponse(
                {
                    "error": "This reset link is invalid or has expired. "
                    "Please request a new one."
                },
                status_code=400,
            )

        # Mark token as used
        try:
            await password_resets.consume_password_reset_token(conn, token)
        except Exception as e:
            print(f"Failed to consume password reset token: {e}")

        # Update the password
        hash_bytes = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        hash_str = hash_bytes.decode()

        try:
            row = await conn.fetchrow(
                "UPDATE users SET password_hash = $1, updated_at = NOW() "
                "WHERE id = $2 "
                "RETURNING id, email, role, organization_id",
                hash_str,
                verified.user_id,
            )
        except Exception:
            return JSONResponse(
                {"error": "Failed to reset password"},
                status_code=500,
            )

        if row is None:
            return JSONResponse(
                {"error": "Failed to reset password"},
                status_code=500,
            )

        email: str = row["email"]
        role: str = row["role"]
        org_id = row["organization_id"] or 0
        save_session(request, verified.user_id, email, role, org_id)
        redirect = "/admin" if role == "super" else "/dashboard"
        return JSONResponse({"ok": True, "redirect": redirect})