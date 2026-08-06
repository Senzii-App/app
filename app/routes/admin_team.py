"""Admin team routes — team management (invite, list, delete) and API key management.
Direct port of src/routes/admin_team.rs (list/invite/magic-link/me) and
src/routes/api_keys.rs (list/create/revoke API keys).

All routes require admin auth (require_auth). org_id comes from the session.
Mounted under /api/admin prefix in main.py.

Per the task spec, the api-key routes are nested under /team/{id}/api-keys:
  GET    /team/{id}/api-keys          — list API keys for a team member
  POST   /team/{id}/api-keys          — create an API key for a team member
  DELETE /team/{id}/api-keys/{key_id} — revoke an API key
"""
import hashlib
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.services.email import send_magic_link_email

router = APIRouter()


# ── Request models ───────────────────────────────────────────────────────────

class InviteAdminPayload(BaseModel):
    name: str
    email: str


class CreateApiKeyPayload(BaseModel):
    label: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

KEY_BYTES = 32


def _generate_api_key() -> str:
    """Generate a random API key string with a `senzii_` prefix."""
    return f"senzii_{secrets.token_hex(KEY_BYTES)}"


def _hash_key(key: str) -> str:
    """Hash an API key for storage with SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


def _resolve_base_url(request: Request) -> str:
    """Resolve base URL from request headers (x-original-host → x-forwarded-host → host)."""
    host = (
        request.headers.get("x-original-host")
        or request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or "localhost:3000"
    )
    host = host.split(",")[0].strip()
    scheme = "http" if host.startswith("localhost") else "https"
    return f"{scheme}://{host}"


def _sql_escape(s: str) -> str:
    """Escape single quotes for simple_query-style SQL."""
    return s.replace("'", "''")


async def _create_admin_magic_token(conn, user_id: int, org_id: int) -> str:
    """Create an admin magic token and return the raw token string.

    Direct port of db/admin_magic_links.rs::create_admin_magic_token.
    7-day expiry, crypto-random token generation.
    """
    from datetime import datetime, timedelta, timezone
    token = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    await conn.execute(
        "INSERT INTO admin_magic_tokens (user_id, organization_id, token, expires_at) "
        "VALUES ($1, $2, $3, $4)",
        user_id, org_id, token, expires_at,
    )
    return token


# ── Team handlers ─────────────────────────────────────────────────────────────

@router.get("/team")
async def list_team(request: Request):
    """GET /team — list all admins/super users in this organization.

    Mirrors Rust admin_team.rs::list_team.
    """
    try:
        _user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, email, role FROM users "
            "WHERE organization_id = $1 AND (role = 'admin' OR role = 'super') "
            "ORDER BY name, email",
            org_id,
        )

    team = [
        {
            "id": r["id"],
            "name": r["name"] or "",
            "email": r["email"],
            "role": r["role"] or "",
        }
        for r in rows
    ]
    return {"team": team}


@router.post("/team")
async def invite_admin(request: Request, payload: InviteAdminPayload):
    """POST /team — invite a new admin by creating a user and sending a magic link.

    Mirrors Rust admin_team.rs::invite_admin.
    """
    try:
        _user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    name = payload.name.strip()
    email = payload.email.strip().lower()

    if not name or not email:
        return JSONResponse({"error": "name and email are required"}, status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure the current org exists (defensive)
        org_row = await conn.fetchrow("SELECT name FROM organizations WHERE id = $1", org_id)
        if org_row is None:
            return JSONResponse({"error": "Organization not found"}, status_code=500)
        org_name = org_row["name"]

        # Check for existing user with this email anywhere
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE LOWER(email) = LOWER($1)",
            email,
        )
        if existing is not None:
            return JSONResponse(
                {"error": "A user with this email already exists"},
                status_code=409,
            )

        # Create the admin user with no password (magic-link-only)
        try:
            user_row = await conn.fetchrow(
                "INSERT INTO users (name, email, password_hash, role, organization_id) "
                "VALUES ($1, $2, NULL, 'admin', $3) RETURNING id, name, email",
                name, email, org_id,
            )
        except Exception as e:
            err = str(e)
            if "23505" in err or "duplicate key" in err:
                return JSONResponse(
                    {"error": "A user with this email already exists"},
                    status_code=409,
                )
            print(f"Invite admin create user error: {e}")
            return JSONResponse({"error": "Failed to create admin user"}, status_code=500)

        user_id = user_row["id"]

        # Create admin magic token
        try:
            token = await _create_admin_magic_token(conn, user_id, org_id)
        except Exception as e:
            print(f"Create admin magic token error for user {user_id} in org {org_id}: {e}")
            return JSONResponse(
                {"error": f"Failed to create magic link: {e}"},
                status_code=500,
            )

        base_url = _resolve_base_url(request)
        link = f"{base_url}/auth/admin-login?token={token}"

        # Send the magic link email
        email_result = await send_magic_link_email(
            to=email,
            magic_link=link,
            org_name=org_name,
        )
        if not email_result.get("sent"):
            print(f"[admin-invite] Email not sent to {email} (reason: {email_result.get('reason')}). Link: {link}")

    return {
        "id": user_id,
        "name": name,
        "email": email,
        "link": link,
        "email_sent": email_result.get("sent", False),
    }


@router.delete("/team/{id}")
async def delete_team_member(request: Request, id: int):
    """DELETE /team/{id} — remove a team member (admin user).

    Removes the admin user from the organization. Only users belonging to
    the same org and with role admin/super can be removed. Super users cannot
    remove themselves (defensive guard).
    """
    try:
        user_id, role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify user belongs to this org and is an admin/super
        target = await conn.fetchrow(
            "SELECT id, name, email, role FROM users "
            "WHERE id = $1 AND organization_id = $2 AND (role = 'admin' OR role = 'super')",
            id, org_id,
        )
        if target is None:
            return JSONResponse({"error": "Admin not found"}, status_code=404)

        # Defensive: don't allow removing yourself
        if id == user_id:
            return JSONResponse(
                {"error": "You cannot remove your own account"},
                status_code=400,
            )

        try:
            await conn.execute("DELETE FROM users WHERE id = $1 AND organization_id = $2", id, org_id)
        except Exception as e:
            print(f"Delete team member error: {e}")
            return JSONResponse({"error": "Failed to delete team member"}, status_code=500)

    return {"ok": True, "message": "Team member removed"}


# ── API key handlers (nested under /team/{id}/api-keys) ──────────────────────

@router.get("/team/{id}/api-keys")
async def list_api_keys(request: Request, id: int):
    """GET /team/{id}/api-keys — list all active API keys for a team member.

    Mirrors Rust api_keys.rs::list_api_keys (db::api_keys::list_api_keys).
    Each user only sees their own keys — not keys created by other admins.
    Returns public-facing structs with external_id UUIDs.
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Only allow listing your own keys (mirrors Rust: user_id scoping)
    if id != user_id:
        return JSONResponse({"error": "API key not found"}, status_code=404)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ak.id, ak.label, ak.last_used_at, ak.created_at,
                      o.external_id AS org_external_id
               FROM api_keys ak
               JOIN organizations o ON o.id = ak.organization_id
               WHERE ak.organization_id = $1 AND ak.user_id = $2 AND ak.revoked_at IS NULL
               ORDER BY ak.created_at DESC""",
            org_id, user_id,
        )

    keys = [
        {
            "id": r["id"],
            "label": r["label"],
            "organization_id": str(r["org_external_id"]),
            "last_used_at": r["last_used_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"api_keys": keys}


@router.post("/team/{id}/api-keys")
async def create_api_key(request: Request, id: int, payload: CreateApiKeyPayload):
    """POST /team/{id}/api-keys — create a new API key for a team member.

    Returns the plaintext key only in this response — it cannot be retrieved later.
    Mirrors Rust api_keys.rs::create_api_key (db::api_keys::create_api_key).
    The response includes the organization's external_id (UUID) for MCP config.
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Only allow creating keys for yourself (mirrors Rust: user_id scoping)
    if id != user_id:
        return JSONResponse({"error": "API key not found"}, status_code=404)

    label = (payload.label or "").strip() if payload.label else ""
    if not label:
        label = "Default"

    plaintext = _generate_api_key()
    key_hash = _hash_key(plaintext)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO api_keys (user_id, organization_id, key_hash, label)
                   VALUES ($1, $2, $3, $4)
                   RETURNING id, user_id, organization_id, label, last_used_at, revoked_at, created_at""",
                user_id, org_id, key_hash, label,
            )
        except Exception as e:
            print(f"Create API key error: {e}")
            return JSONResponse({"error": "Failed to create API key"}, status_code=500)

    return {
        "id": row["id"],
        "label": row["label"],
        "created_at": row["created_at"],
        "plaintext": plaintext,
    }


@router.delete("/team/{id}/api-keys/{key_id}")
async def revoke_api_key(request: Request, id: int, key_id: int):
    """DELETE /team/{id}/api-keys/{key_id} — revoke an API key.

    Only revokes if the key belongs to the given user and org.
    Users can only revoke their own keys — not keys created by other admins.
    Mirrors Rust api_keys.rs::revoke_api_key (db::api_keys::revoke_api_key).
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Only allow revoking your own keys (mirrors Rust: user_id scoping)
    if id != user_id:
        return JSONResponse({"error": "API key not found"}, status_code=404)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE id = $1 AND user_id = $2 AND organization_id = $3 AND revoked_at IS NULL",
                key_id, user_id, org_id,
            )
        except Exception as e:
            print(f"Revoke API key error: {e}")
            return JSONResponse({"error": "Failed to revoke API key"}, status_code=500)

    # asyncpg execute returns "UPDATE N" string; parse the count
    count = 0
    if isinstance(result, str) and result.startswith("UPDATE "):
        try:
            count = int(result.split(" ", 1)[1])
        except Exception:
            count = 0

    if count > 0:
        return {"ok": True}
    return JSONResponse({"error": "API key not found"}, status_code=404)