"""Admin client routes — client CRUD + magic link generation.
Direct port of src/routes/admin_clients.rs.

All routes require admin auth (require_auth). org_id comes from the session.
Mounted under /api/admin prefix in main.py.
"""
import asyncio
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db.audit_log import log as audit_log
from app.services.email import send_client_magic_link_email

router = APIRouter()


# ── Request models ───────────────────────────────────────────────────────────

class CreateClientPayload(BaseModel):
    name: str
    email: str
    phone: str | None = None
    company_name: str | None = None
    # organization_id is rejected if present (mirrors Rust _organization_id)
    organization_id: int | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.get("/clients")
async def list_clients(request: Request):
    """GET /clients — list all clients in the org.

    Mirrors Rust admin_clients.rs::list_clients.
    """
    try:
        _user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.*, COALESCE(sr.req_count, 0) AS request_count
               FROM clients c
               LEFT JOIN LATERAL (SELECT COUNT(*) AS req_count FROM staffing_requests WHERE client_id = c.id AND organization_id = $1) sr ON true
               WHERE c.organization_id = $1
               ORDER BY c.name""",
            org_id,
        )

    clients = [
        {
            "id": r["id"],
            "name": r["name"],
            "email": r["email"],
            "phone": r["phone"],
            "company_name": r["company_name"],
            "organization_id": r["organization_id"],
            "request_count": r["request_count"],
        }
        for r in rows
    ]
    return clients


@router.post("/clients")
async def create_client(request: Request, payload: CreateClientPayload):
    """POST /clients — create a client and send a magic link email.

    Mirrors Rust admin_clients.rs::create_client (simple_query insert, retry fetch,
    audit log) followed by create_client_magic_link (token + email).
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if not payload.name.strip() or not payload.email.strip():
        return JSONResponse({"error": "name and email are required"}, status_code=400)

    if payload.organization_id is not None:
        return JSONResponse(
            {"error": "organization_id cannot be set via request body"},
            status_code=400,
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Neon pooler doesn't reliably return rows from simple_query RETURNING.
        # Use simple_query-style INSERT (no RETURNING), then fetch by unique email+org.
        phone_sql = f"'{_sql_escape(payload.phone)}'" if payload.phone else "NULL"
        company_sql = f"'{_sql_escape(payload.company_name)}'" if payload.company_name else "NULL"
        insert_sql = (
            f"INSERT INTO clients (name, email, phone, company_name, organization_id) "
            f"VALUES ('{_sql_escape(payload.name)}', '{_sql_escape(payload.email)}', {phone_sql}, {company_sql}, {org_id})"
        )
        try:
            await conn.execute(insert_sql)
        except Exception as e:
            err = str(e)
            if "23505" in err or "duplicate key" in err:
                return JSONResponse(
                    {"error": "Client with this email already exists"},
                    status_code=409,
                )
            print(f"Create client error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        # Fetch the newly created client by email (unique per org).
        # Neon read replica may lag behind the write — retry up to 3 times with 150ms delay.
        fetch_sql = "SELECT id, name, email, phone, company_name FROM clients WHERE email = $1 AND organization_id = $2"
        row = None
        for attempt in range(3):
            try:
                row = await conn.fetchrow(fetch_sql, payload.email, org_id)
                if row is not None:
                    break
            except Exception as e:
                if attempt < 2:
                    print(f"Create client fetch attempt {attempt + 1} failed (Neon replication lag): {e}")
                    await asyncio.sleep(0.15)
                else:
                    print(f"Create client fetch error after retries: {e}")
                    return JSONResponse({"error": "Internal server error"}, status_code=500)
            if row is None and attempt < 2:
                await asyncio.sleep(0.15)

        if row is None:
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        client_obj = {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone"],
            "company_name": row["company_name"],
        }

        await audit_log(conn, org_id, user_id, "client.create", "client", client_obj["id"], None, client_obj, None)

        # ── Generate magic link and send email ──────────────────────────────
        # Mirrors admin_clients.rs::create_client_magic_link
        token = str(uuid.uuid4())
        company_sql2 = f"'{_sql_escape(row['company_name'])}'" if row["company_name"] else "NULL"
        # Neon pooler doesn't reliably return rows from simple_query RETURNING —
        # INSERT without RETURNING, then SELECT by token to confirm.
        insert_token_sql = (
            f"INSERT INTO client_magic_tokens (client_id, client_email, client_name, company_name, organization_id, token, expires_at) "
            f"VALUES ({row['id']}, '{_sql_escape(row['email'])}', '{_sql_escape(row['name'])}', {company_sql2}, {org_id}, '{token}', NOW() + INTERVAL '7 days')"
        )
        try:
            await conn.execute(insert_token_sql)
        except Exception as e:
            print(f"Magic link insert error: {e}")
            return JSONResponse({"error": "Failed to create magic link"}, status_code=500)

        # Fetch the row we just inserted by token to get expires_at
        try:
            expires_at = await conn.fetchval(
                "SELECT expires_at::text FROM client_magic_tokens WHERE token = $1",
                token,
            )
        except Exception as e:
            print(f"Magic link select error: {e}")
            return JSONResponse({"error": "Failed to retrieve magic link"}, status_code=500)

        if expires_at is None:
            print("Magic link inserted but not found on SELECT")
            return JSONResponse({"error": "Failed to retrieve magic link"}, status_code=500)

        base_url = _resolve_base_url(request)
        link = f"{base_url}/client/login?token={token}"

        # Look up org name for the email
        org_name_row = await conn.fetchrow("SELECT name FROM organizations WHERE id = $1", org_id)
        org_name = org_name_row["name"] if org_name_row else "your organization"

        # Send the magic link email
        email_result = await send_client_magic_link_email(
            to=row["email"],
            client_name=row["name"],
            magic_link=link,
            org_name=org_name,
        )
        email_sent = email_result.get("sent", False)
        if not email_sent:
            print(f"[magic-link] Client email not sent to {row['email']} (reason: {email_result.get('reason')}). Link: {link}")

        return JSONResponse(
            {
                **client_obj,
                "link": link,
                "expires_at": expires_at,
                "email_sent": email_sent,
                "email": row["email"],
            },
            status_code=201,
        )


@router.delete("/clients/{id}")
async def delete_client(request: Request, id: int):
    """DELETE /clients/{id} — delete a client that has no staffing requests.

    If the client has requests, returns 409 Conflict.
    Mirrors Rust admin_clients.rs::delete_client.
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify client belongs to this org
        client_exists = await conn.fetchval(
            "SELECT id FROM clients WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if client_exists is None:
            return JSONResponse({"error": "Client not found"}, status_code=404)

        # Check for staffing requests referencing this client
        request_count = await conn.fetchval(
            "SELECT COUNT(*) FROM staffing_requests WHERE client_id = $1 AND organization_id = $2",
            id, org_id,
        )

        if request_count and request_count > 0:
            return JSONResponse(
                {"error": f"Cannot delete client — they have {request_count} staffing request(s). Remove or reassign their requests first."},
                status_code=409,
            )

        # Also check for shifts referencing this client (via client_id on shifts)
        shift_count = 0
        try:
            shift_count = await conn.fetchval(
                "SELECT COUNT(*) FROM shifts WHERE client_id = $1 AND organization_id = $2",
                id, org_id,
            )
        except Exception:
            # shifts table may not have client_id — treat as 0
            shift_count = 0

        if shift_count and shift_count > 0:
            return JSONResponse(
                {"error": f"Cannot delete client — they have {shift_count} shift(s). Remove or reassign their shifts first."},
                status_code=409,
            )

        # Safe to delete — client_magic_tokens will cascade
        try:
            await conn.execute(
                f"DELETE FROM clients WHERE id = {id} AND organization_id = {org_id}"
            )
        except Exception as e:
            print(f"Delete client error: {e}")
            return JSONResponse({"error": "Failed to delete client"}, status_code=500)

        before = {"id": id}
        await audit_log(conn, org_id, user_id, "client.delete", "client", id, before, None, None)

    return {"deleted": True}