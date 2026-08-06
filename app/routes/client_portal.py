"""Client portal routes — magic link + password login, profile, work sites,
and staffing requests.

Direct port of src/routes/client_portal.rs from the Rust project.
All SQL queries are copied 1:1 from the Rust source.

Router has no prefix — it's set in main.py as '/client/api'.
"""
import uuid
from datetime import datetime, date, time, timezone

import bcrypt
from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db.pool import get_pool
from app.middleware.auth import (
    require_client_session,
    save_session,
    destroy_session,
    CLIENT_ID_KEY,
    SESSION_START_KEY,
    LAST_ACTIVITY_KEY,
)

router = APIRouter()


# ── Pydantic request models ──────────────────────────────────────────────────


class PasswordLoginPayload(BaseModel):
    email: str
    password: str


class SetPasswordPayload(BaseModel):
    password: str


class CreateStaffingRequestPayload(BaseModel):
    site_id: int | None = None
    shift_date: str
    start_time: str
    end_time: str
    required_skills: list | dict | None = None
    min_staff: int | None = None
    notes: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_date(s: str) -> date:
    """Parse a 'YYYY-MM-DD' string into a date object."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Invalid shift_date format, expected YYYY-MM-DD")


def _parse_time_hhmm(s: str) -> time:
    """Parse a 'HH:MM' string into a time object."""
    try:
        return datetime.strptime(s, "%H:%M").time()
    except ValueError:
        raise ValueError("Invalid time format, expected HH:MM")


async def _list_org_certs(conn, org_id: int) -> list[dict]:
    """List organization certifications (id + name) for a given org."""
    rows = await conn.fetch(
        "SELECT id, name FROM organization_certifications WHERE organization_id = $1 ORDER BY name",
        org_id,
    )
    return [{"id": r["id"], "name": r["name"]} for r in rows]


# ── Handlers ──────────────────────────────────────────────────────────────────


@router.get("/login")
async def get_login_magic_link(request: Request, token: str | None = Query(default=None)):
    """GET /login?token=... — magic link login for clients."""
    if not token:
        raise HTTPException(status_code=400, detail="Token required")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify magic token (client_magic_tokens carries the client snapshot)
        row = await conn.fetchrow(
            """
            SELECT cmt.client_id, cmt.client_email, cmt.client_name,
                   cmt.company_name, cmt.organization_id
            FROM client_magic_tokens cmt
            WHERE cmt.token = $1 AND cmt.expires_at > NOW()
            """,
            token,
        )
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid or expired link")

        client_id = row["client_id"]
        client_email = row["client_email"]
        client_name = row["client_name"]
        company_name = row["company_name"]
        oid = row["organization_id"] or 0

        # Consume token
        await conn.execute("DELETE FROM client_magic_tokens WHERE token = $1", token)

        # Get or create user linked to this client record
        user_row = await conn.fetchrow(
            "SELECT id, email, password_hash, organization_id FROM users WHERE email = $1 AND role = 'client'",
            client_email,
        )
        if user_row is not None:
            user_id = user_row["id"]
            has_password = bool(user_row["password_hash"])
        else:
            # Create user for this client
            temp_password = str(uuid.uuid4())
            pw_hash = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()
            try:
                user_id = await conn.fetchval(
                    "INSERT INTO users (email, password_hash, role, organization_id) VALUES ($1, $2, 'client', $3) RETURNING id",
                    client_email,
                    pw_hash,
                    oid,
                )
            except Exception:
                raise HTTPException(status_code=500, detail="Failed to create user")
            # Link user to client record
            await conn.execute("UPDATE clients SET user_id = $1 WHERE id = $2", user_id, client_id)
            has_password = False

    # Save session
    session = request.session
    now = int(datetime.now(timezone.utc).timestamp())
    save_session(request, user_id, client_email, "client", oid)
    session[CLIENT_ID_KEY] = client_id
    session[SESSION_START_KEY] = now
    session[LAST_ACTIVITY_KEY] = now

    return {
        "ok": True,
        "client": {
            "id": client_id,
            "name": client_name,
            "email": client_email,
            "company_name": company_name,
        },
        "hasPassword": has_password,
    }


@router.post("/login")
async def post_login_password(request: Request, payload: PasswordLoginPayload):
    """POST /login — email + password login for clients."""
    email = payload.email.strip().lower()
    password = payload.password

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.email, u.password_hash, u.role, u.organization_id, c.id AS client_id
            FROM users u
            JOIN clients c ON c.user_id = u.id
            WHERE u.email = $1 AND u.role = 'client'
            """,
            email,
        )
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        pw_hash = row["password_hash"]
        if not pw_hash:
            raise HTTPException(
                status_code=401,
                detail="No password set. Use the magic link to sign in first.",
            )

        if not bcrypt.checkpw(password.encode(), pw_hash.encode()):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        uid = row["id"]
        uemail = row["email"]
        client_id = row["client_id"]
        org_id = row["organization_id"] or 0

        # Save session
        session = request.session
        now = int(datetime.now(timezone.utc).timestamp())
        save_session(request, uid, uemail, "client", org_id)
        session[CLIENT_ID_KEY] = client_id
        session[SESSION_START_KEY] = now
        session[LAST_ACTIVITY_KEY] = now

        # Fetch full client profile to return with login response
        profile_row = await conn.fetchrow(
            "SELECT * FROM clients WHERE id = $1 AND organization_id = $2",
            client_id,
            org_id,
        )
        profile = None
        if profile_row is not None:
            profile = {
                "id": profile_row["id"],
                "name": profile_row["name"],
                "email": profile_row["email"],
                "phone": profile_row["phone"],
                "company_name": profile_row["company_name"],
                "organization_id": profile_row["organization_id"],
            }

    resp = {"ok": True, "hasPassword": True}
    if profile is not None:
        resp["profile"] = profile
    return resp


@router.get("/set-password")
async def get_set_password_form(request: Request, token: str | None = Query(default=None)):
    """GET /set-password?token=... — validate the set-password token.

    The Rust app serves an HTML page here; for the API port we just validate
    the token and return whether it's valid so the frontend can render the form.
    """
    if not token:
        raise HTTPException(status_code=400, detail="Token required")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT client_id, expires_at FROM client_magic_tokens WHERE token = $1 AND expires_at > NOW()",
            token,
        )
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired link")

    return {"ok": True, "valid": True}


@router.post("/set-password")
async def post_set_password(request: Request, payload: SetPasswordPayload):
    """POST /set-password — set password for client."""
    user_id, _org_id, _client_id = require_client_session(request)

    if len(payload.password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters",
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        pw_hash = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt()).decode()
        await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2",
            pw_hash,
            user_id,
        )

    return {"ok": True}


@router.get("/logout")
async def get_logout(request: Request):
    """GET /logout — destroy the client session."""
    destroy_session(request)
    return {"ok": True}


@router.post("/logout")
async def post_logout(request: Request):
    """POST /logout — destroy the client session (alias)."""
    destroy_session(request)
    return {"ok": True}


@router.get("/me")
async def get_me(request: Request):
    """GET /me — get client profile."""
    _user_id, org_id, client_id = require_client_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM clients WHERE id = $1 AND organization_id = $2",
            client_id,
            org_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Client not found")

    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "company_name": row["company_name"],
        "organization_id": row["organization_id"],
    }


@router.patch("/me")
async def patch_me(request: Request, payload: dict):
    """PATCH /me — update client profile.

    The Rust client_portal.rs does not define a PATCH /me handler, but the
    task spec requires one. We support updating name, phone, and company_name,
    matching the client profile fields.
    """
    _user_id, org_id, client_id = require_client_session(request)

    updates: list[str] = []
    params: list = []
    idx = 1
    for field in ("name", "phone", "company_name"):
        if field in payload and payload[field] is not None:
            updates.append(f"{field} = ${idx}")
            params.append(payload[field])
            idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    updates.append("updated_at = NOW()")
    params.append(client_id)
    params.append(org_id)
    sql = (
        f"UPDATE clients SET {', '.join(updates)} "
        f"WHERE id = ${idx} AND organization_id = ${idx + 1} "
        "RETURNING id, name, email, phone, company_name, organization_id"
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)

    if row is None:
        raise HTTPException(status_code=404, detail="Client not found")

    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "company_name": row["company_name"],
        "organization_id": row["organization_id"],
    }


@router.get("/work-sites")
async def get_work_sites(request: Request):
    """GET /work-sites — list work sites for the client's org."""
    _user_id, org_id, _client_id = require_client_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, address,
                   COALESCE(latitude, 0)::double precision AS latitude,
                   COALESCE(longitude, 0)::double precision AS longitude,
                   required_skills, timezone
            FROM work_sites
            WHERE organization_id = $1
            ORDER BY name
            """,
            org_id,
        )

    return [
        {
            "id": r["id"],
            "name": r["name"],
            "address": r["address"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "required_skills": r["required_skills"],
            "timezone": r["timezone"],
        }
        for r in rows
    ]


@router.post("/requests")
async def create_request(request: Request, payload: CreateStaffingRequestPayload):
    """POST /requests — create a staffing request."""
    _user_id, org_id, client_id = require_client_session(request)

    if not payload.shift_date or not payload.start_time or not payload.end_time:
        raise HTTPException(
            status_code=400,
            detail="shift_date, start_time, and end_time are required",
        )

    try:
        shift_date = _parse_date(payload.shift_date)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid shift_date format, expected YYYY-MM-DD",
        )
    try:
        start_time = _parse_time_hhmm(payload.start_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start_time format, expected HH:MM")
    try:
        end_time = _parse_time_hhmm(payload.end_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid end_time format, expected HH:MM")

    pool = await get_pool()
    async with pool.acquire() as conn:
        import json

        skills = json.dumps(payload.required_skills) if payload.required_skills is not None else "[]"
        min_staff = payload.min_staff if payload.min_staff is not None else 1

        row = await conn.fetchrow(
            """
            INSERT INTO staffing_requests
                (client_id, site_id, shift_date, start_time, end_time,
                 required_skills, min_staff, notes, organization_id, status)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, 'open')
            RETURNING id, client_id, site_id, shift_date::text AS shift_date,
                      start_time::text AS start_time, end_time::text AS end_time,
                      required_skills, min_staff, notes, status, organization_id
            """,
            client_id,
            payload.site_id,
            shift_date,
            start_time,
            end_time,
            skills,
            min_staff,
            payload.notes,
            org_id,
        )

    return JSONResponse(
        status_code=201,
        content={
            "id": row["id"],
            "client_id": row["client_id"],
            "site_id": row["site_id"],
            "shift_date": row["shift_date"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "required_skills": row["required_skills"],
            "min_staff": row["min_staff"],
            "notes": row["notes"],
            "status": row["status"],
            "organization_id": row["organization_id"],
        },
    )


@router.get("/requests")
async def list_requests(request: Request):
    """GET /requests — list the client's staffing requests."""
    _user_id, org_id, client_id = require_client_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, client_id, site_id, shift_date::text AS shift_date,
                   start_time::text AS start_time, end_time::text AS end_time,
                   required_skills, min_staff, notes, status, organization_id
            FROM staffing_requests
            WHERE client_id = $1 AND organization_id = $2
            ORDER BY created_at DESC
            """,
            client_id,
            org_id,
        )

    return [
        {
            "id": r["id"],
            "client_id": r["client_id"],
            "site_id": r["site_id"],
            "shift_date": r["shift_date"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "required_skills": r["required_skills"],
            "min_staff": r["min_staff"],
            "notes": r["notes"],
            "status": r["status"],
            "organization_id": r["organization_id"],
        }
        for r in rows
    ]