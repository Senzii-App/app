"""Admin staff routes — staff CRUD + magic link generation.
Direct port of src/routes/admin_staff.rs and the staff CRUD handlers
from src/routes/staff.rs (list/get/create/delete).

All routes require admin auth (require_auth). org_id comes from the session.
Mounted under /api/admin prefix in main.py.
"""
import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db.audit_log import log as audit_log
from app.services.email import send_staff_magic_link_email
from app.services.geocode import geocode_address

router = APIRouter()


# ── Request models ───────────────────────────────────────────────────────────

class CreateStaffPayload(BaseModel):
    name: str
    email: str
    phone: str | None = None
    address: str | None = None
    timezone: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_base_url(request: Request) -> str:
    """Resolve base URL from request headers (x-original-host → x-forwarded-host → host)."""
    host = (
        request.headers.get("x-original-host")
        or request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or "localhost:3000"
    )
    # Take the first host if comma-separated
    host = host.split(",")[0].strip()
    scheme = "http" if host.startswith("localhost") else "https"
    return f"{scheme}://{host}"


def _sql_escape(s: str) -> str:
    """Escape single quotes for simple_query-style SQL."""
    return s.replace("'", "''")


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.get("/staff")
async def list_staff(request: Request):
    """GET /staff — list all staff in the org.

    Mirrors Rust staff.rs::list_staff. Returns { "staff": [...] }.
    """
    try:
        _user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.id, s.name, s.email, s.phone, s.address,
                      COALESCE(s.latitude, 0)::double precision AS latitude,
                      COALESCE(s.longitude, 0)::double precision AS longitude,
                      s.timezone, s.organization_id,
                      COALESCE(certs.c, '[]'::json) as certifications,
                      COALESCE(avail.a, '[]'::json) as availability
               FROM staff s
               LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sc.id, 'name', sc.name, 'expires_at', sc.expires_at)) as c
                                  FROM staff_certifications sc WHERE sc.staff_id = s.id) certs ON true
               LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sa.id, 'day_of_week', sa.day_of_week, 'start_time', sa.start_time::text, 'end_time', sa.end_time::text)) as a
                                  FROM staff_availability sa WHERE sa.staff_id = s.id) avail ON true
               WHERE s.organization_id = $1 AND s.deleted_at IS NULL
               ORDER BY s.name""",
            org_id,
        )

    staff = [
        {
            "id": r["id"],
            "name": r["name"],
            "email": r["email"],
            "phone": r["phone"],
            "address": r["address"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "timezone": r["timezone"],
            "organization_id": r["organization_id"],
            "certifications": r["certifications"],
            "availability": r["availability"],
        }
        for r in rows
    ]
    return {"staff": staff}


@router.get("/staff/{id}")
async def get_staff(request: Request, id: int):
    """GET /staff/{id} — get a single staff member.

    Mirrors Rust staff.rs::get_staff.
    """
    try:
        _user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.id, s.name, s.email, s.phone, s.address,
                      COALESCE(s.latitude, 0)::double precision AS latitude,
                      COALESCE(s.longitude, 0)::double precision AS longitude,
                      s.timezone, s.organization_id,
                      COALESCE(certs.c, '[]'::json) as certifications,
                      COALESCE(avail.a, '[]'::json) as availability
               FROM staff s
               LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sc.id, 'name', sc.name, 'expires_at', sc.expires_at)) as c
                                  FROM staff_certifications sc WHERE sc.staff_id = s.id) certs ON true
               LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sa.id, 'day_of_week', sa.day_of_week, 'start_time', sa.start_time::text, 'end_time', sa.end_time::text)) as a
                                  FROM staff_availability sa WHERE sa.staff_id = s.id) avail ON true
               WHERE s.id = $1 AND s.organization_id = $2 AND s.deleted_at IS NULL""",
            id, org_id,
        )

    if row is None:
        return JSONResponse({"error": "Staff not found"}, status_code=404)

    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "address": row["address"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
        "organization_id": row["organization_id"],
        "certifications": row["certifications"],
        "availability": row["availability"],
    }


@router.post("/staff")
async def create_staff(request: Request, payload: CreateStaffPayload):
    """POST /staff — create a staff member and send a magic link email.

    Mirrors Rust staff.rs::create_staff (seat check, geocode, simple_query insert,
    retry fetch) followed by admin_staff.rs::create_staff_magic_link (token + email).
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if not payload.name.strip() or not payload.email.strip():
        return JSONResponse({"error": "name and email are required"}, status_code=400)

    # Resolve coordinates from address via geocoding
    latitude = None
    longitude = None
    if payload.address and payload.address.strip():
        coords = await geocode_address(payload.address)
        if coords:
            latitude = coords.latitude
            longitude = coords.longitude

    pool = await get_pool()
    async with pool.acquire() as conn:
        # ── Seat reservation check ─────────────────────────────────────────
        # If org has seats > 0 (Stripe-billed), staff count cannot exceed seats.
        # Orgs with seats=0 (pre-Stripe) are grandfathered — unlimited.
        seat_row = await conn.fetchrow(
            """SELECT o.seats,
                      (SELECT COUNT(*)::int FROM staff WHERE organization_id = $1 AND deleted_at IS NULL) AS staff_count
               FROM organizations o WHERE o.id = $1""",
            org_id,
        )
        if seat_row is not None:
            seats = seat_row["seats"]
            staff_count = seat_row["staff_count"]
            if seats > 0 and staff_count >= seats:
                return JSONResponse(
                    {
                        "error": "staff_limit_reached",
                        "message": f"You have {seats} reserved staff members and {staff_count} in use. Add more in Settings → Staff Members to add additional staff.",
                    },
                    status_code=402,
                )

        timezone_str = payload.timezone or "UTC"

        # Neon pooler doesn't reliably return rows from simple_query RETURNING.
        # Use simple_query-style INSERT (no RETURNING), then fetch by unique email.
        phone_sql = f"'{_sql_escape(payload.phone)}'" if payload.phone else "NULL"
        address_sql = f"'{_sql_escape(payload.address)}'" if payload.address else "NULL"
        lat_sql = str(latitude) if latitude is not None else "NULL"
        lon_sql = str(longitude) if longitude is not None else "NULL"
        insert_sql = (
            f"INSERT INTO staff (name, email, phone, address, latitude, longitude, timezone, organization_id) "
            f"VALUES ('{_sql_escape(payload.name)}', '{_sql_escape(payload.email)}', {phone_sql}, {address_sql}, {lat_sql}, {lon_sql}, '{_sql_escape(timezone_str)}', {org_id})"
        )
        try:
            await conn.execute(insert_sql)
        except Exception as e:
            err = str(e)
            if "23505" in err or "duplicate key" in err:
                return JSONResponse({"error": "Staff with this email already exists"}, status_code=409)
            print(f"Create staff error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        # Fetch the newly created staff by email (unique per org).
        # Neon read replica may lag behind the write — retry up to 5 times with 300ms delay.
        fetch_sql = (
            "SELECT id, name, email, phone, address, "
            "COALESCE(latitude, 0)::double precision AS latitude, "
            "COALESCE(longitude, 0)::double precision AS longitude, "
            "timezone, organization_id "
            "FROM staff WHERE email = $1 AND organization_id = $2 AND deleted_at IS NULL"
        )
        row = None
        for attempt in range(5):
            try:
                row = await conn.fetchrow(fetch_sql, payload.email, org_id)
                if row is not None:
                    break
            except Exception as e:
                if attempt < 4:
                    print(f"Create staff fetch attempt {attempt + 1} failed (Neon replication lag): {e}")
                    await asyncio.sleep(0.3)
                else:
                    print(f"Create staff fetch error after retries: {e}")
                    return JSONResponse({"error": "Internal server error"}, status_code=500)
            if row is None and attempt < 4:
                await asyncio.sleep(0.3)

        if row is None:
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        staff_id = row["id"]
        staff_obj = {
            "id": staff_id,
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone"],
            "address": row["address"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "timezone": row["timezone"],
            "organization_id": row["organization_id"],
        }

        await audit_log(conn, org_id, user_id, "staff.create", "staff", staff_id, None, staff_obj, None)

        # ── Generate magic link and send email ──────────────────────────────
        # Mirrors admin_staff.rs::create_staff_magic_link
        token = secrets.token_hex(32)
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)

        try:
            magic_row = await conn.fetchrow(
                "INSERT INTO magic_tokens (staff_id, token, expires_at) VALUES ($1, $2, $3) RETURNING token, expires_at",
                staff_id, token, expires_at,
            )
        except Exception as e:
            print(f"Create magic token error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        token_str = magic_row["token"]
        magic_expires_at = magic_row["expires_at"]

        base_url = _resolve_base_url(request)
        link = f"{base_url}/staff/login?token={token_str}"

        # Look up org name for the email
        org_name_row = await conn.fetchrow("SELECT name FROM organizations WHERE id = $1", org_id)
        org_name = org_name_row["name"] if org_name_row else "your organization"

        # Send the magic link email
        email_result = await send_staff_magic_link_email(
            to=payload.email,
            staff_name=payload.name,
            magic_link=link,
            org_name=org_name,
        )
        email_sent = email_result.get("sent", False)
        if not email_sent:
            print(f"[magic-link] Staff email not sent to {payload.email} (reason: {email_result.get('reason')}). Link: {link}")

        return JSONResponse(
            {
                **staff_obj,
                "link": link,
                "expires_at": magic_expires_at.isoformat() if magic_expires_at else None,
                "email_sent": email_sent,
                "email": payload.email,
            },
            status_code=201,
        )


@router.delete("/staff/{id}")
async def delete_staff(request: Request, id: int):
    """DELETE /staff/{id} — soft-delete a staff member.

    Sets deleted_at = NOW() instead of removing the row.
    Blocked if the staff member has active (confirmed) assignments.
    Mirrors Rust staff.rs::delete_staff.
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify staff exists, belongs to org, and is not already soft-deleted
        existing = await conn.fetchrow(
            "SELECT id, deleted_at FROM staff WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if existing is None:
            return JSONResponse({"error": "Staff not found"}, status_code=404)
        if existing["deleted_at"] is not None:
            return JSONResponse({"error": "Staff member already deleted"}, status_code=409)

        # Check for upcoming or in-progress confirmed assignments — block deletion if any exist
        assignment_count = await conn.fetchval(
            """SELECT COUNT(*) FROM assignments a
               JOIN shifts sh ON sh.id = a.shift_id
               WHERE a.staff_id = $1 AND a.organization_id = $2
                 AND a.status = 'confirmed'
                 AND sh.end_time > NOW()""",
            id, org_id,
        )

        if assignment_count and assignment_count > 0:
            return JSONResponse(
                {"error": f"Cannot delete staff — {assignment_count} active assignment(s). Remove or reassign first."},
                status_code=409,
            )

        # Soft-delete: set deleted_at = NOW()
        # Use simple_query-style SQL to avoid Neon pooler Option type issues
        try:
            await conn.execute(
                f"UPDATE staff SET deleted_at = NOW(), updated_at = NOW() WHERE id = {id} AND organization_id = {org_id}"
            )
        except Exception as e:
            print(f"Soft-delete staff error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        before = {"id": id, "deleted_at": None}
        after = {"id": id, "deleted_at": "now"}
        await audit_log(conn, org_id, user_id, "staff.delete", "staff", id, before, after, None)

    return {"ok": True, "message": "Staff member archived"}