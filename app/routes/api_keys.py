"""API keys route — admin API key management for MCP server authentication.

Direct port of src/routes/api_keys.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Router has prefix='' and routes mount under /api/settings:
  GET    /api-keys       — list all active API keys for the org
  POST   /api-keys       — create a new API key (plaintext shown once)
  DELETE /api-keys/{id}  — revoke an API key
"""
import hashlib
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool

router = APIRouter(prefix="")


# ── Request types ─────────────────────────────────────────────────────────────


class CreateApiKeyPayload(BaseModel):
    label: str | None = None


# ── Helpers (mirror db::api_keys in Rust) ─────────────────────────────────────

KEY_BYTES = 32  # 32 random bytes → 64 hex chars


def generate_api_key() -> str:
    """Generate a random API key string with a `senzii_` prefix."""
    return f"senzii_{secrets.token_hex(KEY_BYTES)}"


def hash_key(key: str) -> str:
    """Hash an API key for storage (SHA-256, hex-encoded)."""
    return hashlib.sha256(key.encode()).hexdigest()


# ── Handlers ─────────────────────────────────────────────────────────────────


@router.get("/api-keys")
async def list_api_keys(request: Request):
    """GET /api-keys — list all active API keys for the org.

    Each user only sees their own keys — not keys created by other admins.
    Returns public-facing structs with external_id UUIDs.
    """
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

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

    return JSONResponse({"api_keys": keys})


@router.post("/api-keys")
async def create_api_key(payload: CreateApiKeyPayload, request: Request):
    """POST /api-keys — create a new API key.

    Returns the plaintext key only in this response — it cannot be retrieved later.
    The response includes the organization's external_id (UUID) for MCP config.
    """
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    label = (payload.label or "").strip()
    label = label if label else "Default"

    plaintext = generate_api_key()
    key_hash = hash_key(plaintext)

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
            print(f"[api_keys] Failed to create API key: {e}")
            return JSONResponse(
                {"error": "Failed to create API key"}, status_code=500
            )

        return JSONResponse(
            {
                "id": row["id"],
                "label": row["label"],
                "created_at": row["created_at"],
                "plaintext": plaintext,
            }
        )


@router.delete("/api-keys/{id}")
async def revoke_api_key(id: int, request: Request):
    """DELETE /api-keys/{id} — revoke an API key.

    Only revokes if the key belongs to the given user and org.
    Users can only revoke their own keys — not keys created by other admins.
    """
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            result = await conn.execute(
                """UPDATE api_keys SET revoked_at = NOW()
                   WHERE id = $1 AND user_id = $2 AND organization_id = $3
                     AND revoked_at IS NULL""",
                id, user_id, org_id,
            )
        except Exception as e:
            print(f"[api_keys] Failed to revoke API key: {e}")
            return JSONResponse(
                {"error": "Failed to revoke API key"}, status_code=500
            )

        # asyncpg returns "UPDATE N" — parse the count
        n = int(result.split()[-1]) if result else 0
        if n == 0:
            return JSONResponse({"error": "API key not found"}, status_code=404)

    return JSONResponse({"ok": True})