"""Staff CRUD endpoints (org-scoped) — FastAPI port of src/routes/staff.rs.

All routes require admin auth. org_id comes exclusively from the session.
All SQL queries are identical to the Rust implementation.
"""
import asyncio
import json
from datetime import datetime, time as dt_time, date as dt_date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db.audit_log import log as audit_log
from app.services.geocode import geocode_address

router = APIRouter()


# ── Request payloads ───────────────────────────────────────────────────────────

class CreateStaffPayload(BaseModel):
    name: str
    email: str
    phone: str | None = None
    address: str | None = None
    timezone: str | None = None


class UpdateStaffPayload(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    timezone: str | None = None


class AddCertificationPayload(BaseModel):
    name: str
    expires_at: str | None = None


class AddAvailabilityPayload(BaseModel):
    day_of_week: int
    start_time: str
    end_time: str


class GeocodeStaffPayload(BaseModel):
    address: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape single quotes for simple_query SQL (matches Rust's esc)."""
    return s.replace("'", "''")


def _parse_json_col(val):
    """Parse a JSON/JSONB column value from asyncpg (str or None) to Python."""
    if val is None:
        return None
    if isinstance(val, str):
        return json.loads(val)
    return val  # already decoded


# ── Handlers ───────────────────────────────────────────────────────────────────

# GET / — list all staff in the org
@router.get("/")
async def list_staff(request: Request):
    _, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.id, s.name, s.email, s.phone, s.address, COALESCE(s.latitude, 0)::double precision AS latitude, COALESCE(s.longitude, 0)::double precision AS longitude, s.timezone, s.organization_id, COALESCE(certs.c, '[]'::json) as certifications, COALESCE(avail.a, '[]'::json) as availability
         FROM staff s
         LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sc.id, 'name', sc.name, 'expires_at', sc.expires_at)) as c FROM staff_certifications sc WHERE sc.staff_id = s.id) certs ON true
         LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sa.id, 'day_of_week', sa.day_of_week, 'start_time', sa.start_time::text, 'end_time', sa.end_time::text)) as a FROM staff_availability sa WHERE sa.staff_id = s.id) avail ON true
         WHERE s.organization_id = $1 AND s.deleted_at IS NULL ORDER BY s.name""",
            org_id,
        )
        staff = []
        for r in rows:
            staff.append({
                "id": r["id"],
                "name": r["name"],
                "email": r["email"],
                "phone": r["phone"],
                "address": r["address"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "timezone": r["timezone"],
                "organization_id": r["organization_id"],
                "certifications": _parse_json_col(r["certifications"]),
                "availability": _parse_json_col(r["availability"]),
            })
        return {"staff": staff}


# GET /timezones — list supported timezones
@router.get("/timezones")
async def list_timezones(request: Request):
    require_auth(request)
    zones = [
        "UTC", "US/Eastern", "US/Central", "US/Mountain", "US/Pacific",
        "US/Alaska", "US/Hawaii", "Europe/London", "Europe/Paris", "Europe/Berlin",
        "Asia/Tokyo", "Asia/Shanghai", "Asia/Kolkata", "Australia/Sydney",
        "Canada/Atlantic", "Canada/Newfoundland", "America/Sao_Paulo",
        "America/Argentina/Buenos_Aires", "Africa/Cairo", "Africa/Lagos",
        "Asia/Dubai", "Asia/Singapore", "Pacific/Auckland",
    ]
    return {"timezones": zones}


# GET /{id} — get a single staff member
@router.get("/{id}")
async def get_staff(request: Request, id: int):
    _, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.id, s.name, s.email, s.phone, s.address, COALESCE(s.latitude, 0)::double precision AS latitude, COALESCE(s.longitude, 0)::double precision AS longitude, s.timezone, s.organization_id, COALESCE(certs.c, '[]'::json) as certifications, COALESCE(avail.a, '[]'::json) as availability
         FROM staff s
         LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sc.id, 'name', sc.name, 'expires_at', sc.expires_at)) as c FROM staff_certifications sc WHERE sc.staff_id = s.id) certs ON true
         LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sa.id, 'day_of_week', sa.day_of_week, 'start_time', sa.start_time::text, 'end_time', sa.end_time::text)) as a FROM staff_availability sa WHERE sa.staff_id = s.id) avail ON true
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
            "certifications": _parse_json_col(row["certifications"]),
            "availability": _parse_json_col(row["availability"]),
        }


# POST / — create a staff member
@router.post("/")
async def create_staff(request: Request, payload: CreateStaffPayload):
    user_id, _, _, org_id = require_auth(request)

    if not payload.name or not payload.email:
        return JSONResponse({"error": "name and email are required"}, status_code=400)

    # Resolve coordinates from address via geocoding
    latitude, longitude = None, None
    if payload.address and payload.address.strip():
        coords = await geocode_address(payload.address)
        if coords:
            latitude, longitude = coords.latitude, coords.longitude
        else:
            print("[staff] Could not geocode address, storing without coordinates")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # ── Staff member reservation check ─────────────────────────────────────
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
                return JSONResponse({
                    "error": "staff_limit_reached",
                    "message": f"You have {seats} reserved staff members and {staff_count} in use. Add more in Settings → Staff Members to add additional staff.",
                }, status_code=402)

        timezone = payload.timezone or "UTC"

        # Neon pooler doesn't reliably return rows from simple_query RETURNING.
        # Use simple_query for INSERT (no RETURNING), then fetch by unique email.
        phone_sql = f"'{_esc(payload.phone)}'" if payload.phone else "NULL"
        address_sql = f"'{_esc(payload.address)}'" if payload.address else "NULL"
        lat_sql = str(latitude) if latitude is not None else "NULL"
        lon_sql = str(longitude) if longitude is not None else "NULL"
        insert_sql = (
            f"INSERT INTO staff (name, email, phone, address, latitude, longitude, timezone, organization_id) "
            f"VALUES ('{_esc(payload.name)}', '{_esc(payload.email)}', {phone_sql}, {address_sql}, {lat_sql}, {lon_sql}, '{_esc(timezone)}', {org_id})"
        )
        try:
            await conn.execute(insert_sql)
        except Exception as e:
            err_str = str(e)
            if "23505" in err_str or "duplicate key" in err_str:
                return JSONResponse({"error": "Staff with this email already exists"}, status_code=409)
            print(f"Create staff error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        # Fetch the newly created staff by email (unique per org).
        # Neon read replica may lag behind the write — retry up to 5 times with 300ms delay.
        fetch_sql = (
            "SELECT id, name, email, phone, address, COALESCE(latitude, 0)::double precision AS latitude, "
            "COALESCE(longitude, 0)::double precision AS longitude, timezone, organization_id "
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

        if row is not None:
            staff_id = row["id"]
            staff = {
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
            await audit_log(conn, org_id, user_id, "staff.create", "staff", staff_id, None, staff)
            return JSONResponse(staff, status_code=201)

        return JSONResponse({"error": "Internal server error"}, status_code=500)


# PATCH /{id} — update a staff member
@router.patch("/{id}")
async def update_staff(request: Request, id: int, payload: UpdateStaffPayload):
    user_id, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify staff belongs to org
        existing = await conn.fetchval(
            "SELECT id FROM staff WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL",
            id, org_id,
        )
        if existing is None:
            return JSONResponse({"error": "Staff not found"}, status_code=404)

        # If address is provided, geocode it to update coordinates
        resolved_lat, resolved_lng = None, None
        if payload.address and payload.address.strip():
            coords = await geocode_address(payload.address)
            if coords:
                resolved_lat, resolved_lng = coords.latitude, coords.longitude
            else:
                print("[staff] Could not geocode address during update")

        # Build dynamic UPDATE
        updates = []
        params = []
        idx = 1

        if payload.name is not None:
            updates.append(f"name = ${idx}")
            params.append(payload.name)
            idx += 1
        if payload.email is not None:
            updates.append(f"email = ${idx}")
            params.append(payload.email)
            idx += 1
        if payload.phone is not None:
            updates.append(f"phone = ${idx}")
            params.append(payload.phone)
            idx += 1
        if payload.address is not None:
            updates.append(f"address = ${idx}")
            params.append(payload.address)
            idx += 1
        if resolved_lat is not None:
            updates.append(f"latitude = ${idx}::float8")
            params.append(resolved_lat)
            idx += 1
        if resolved_lng is not None:
            updates.append(f"longitude = ${idx}::float8")
            params.append(resolved_lng)
            idx += 1
        if payload.timezone is not None:
            updates.append(f"timezone = ${idx}")
            params.append(payload.timezone)
            idx += 1

        if not updates:
            return JSONResponse({"error": "Nothing to update"}, status_code=400)

        updates.append("updated_at = NOW()")
        sql = (
            f"UPDATE staff SET {', '.join(updates)} "
            f"WHERE id = ${idx} AND organization_id = ${idx + 1} "
            f"RETURNING id, name, email, phone, address, "
            f"COALESCE(latitude, 0)::double precision AS latitude, "
            f"COALESCE(longitude, 0)::double precision AS longitude, "
            f"timezone, organization_id"
        )
        params.append(id)
        params.append(org_id)

        try:
            row = await conn.fetchrow(sql, *params)
        except Exception as e:
            print(f"Update staff error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        if row is None:
            return JSONResponse({"error": "Staff not found"}, status_code=404)

        staff = {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone"],
            "address": row["address"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "timezone": row["timezone"],
            "organization_id": row["organization_id"],
        }
        await audit_log(conn, org_id, user_id, "staff.update", "staff", id, None, staff)
        return staff


# POST /{id}/certifications — add a certification to a staff member
@router.post("/{id}/certifications")
async def add_certification(request: Request, id: int, payload: AddCertificationPayload):
    user_id, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify staff belongs to org
        existing = await conn.fetchval(
            "SELECT id FROM staff WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL",
            id, org_id,
        )
        if existing is None:
            return JSONResponse({"error": "Staff not found"}, status_code=404)

        if not payload.name:
            return JSONResponse({"error": "name is required"}, status_code=400)

        # Validate: cert name must exist in the org's certification list
        cert_id = await conn.fetchval(
            "SELECT id FROM organization_certifications WHERE organization_id = $1 AND name = $2",
            org_id, payload.name,
        )
        if cert_id is None:
            return JSONResponse({"error": "Unknown certification. Add it to your org's certification list first."}, status_code=400)

        # Parse expires_at — accept date-only ("2027-06-15") or full ISO timestamp
        expires_at = None
        if payload.expires_at and payload.expires_at.strip():
            s = payload.expires_at.strip()
            parsed = False
            # Try full ISO timestamp (with timezone)
            try:
                expires_at = datetime.fromisoformat(s)
                parsed = True
            except ValueError:
                pass
            if not parsed:
                # Try date-only
                try:
                    nd = dt_date.fromisoformat(s)
                    expires_at = datetime(nd.year, nd.month, nd.day, 0, 0, 0)
                    parsed = True
                except ValueError:
                    pass
            if not parsed:
                return JSONResponse({"error": "Invalid expires_at format. Use YYYY-MM-DD or ISO 8601 timestamp."}, status_code=400)

        try:
            row = await conn.fetchrow(
                """INSERT INTO staff_certifications (staff_id, name, expires_at, cert_id)
                   VALUES ($1, $2, $3, $4)
                   RETURNING id, staff_id, name, expires_at::text""",
                id, payload.name, expires_at, cert_id,
            )
        except Exception as e:
            print(f"Add certification error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        cert = {
            "id": row["id"],
            "staff_id": row["staff_id"],
            "name": row["name"],
            "expires_at": row["expires_at"],
        }
        await audit_log(conn, org_id, user_id, "staff_certification.add", "staff_certification", row["id"], None, cert)
        return JSONResponse(cert, status_code=201)


# POST /{id}/availability — add availability window to a staff member
@router.post("/{id}/availability")
async def add_availability(request: Request, id: int, payload: AddAvailabilityPayload):
    _, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify staff belongs to org
        existing = await conn.fetchval(
            "SELECT id FROM staff WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL",
            id, org_id,
        )
        if existing is None:
            return JSONResponse({"error": "Staff not found"}, status_code=404)

        # Parse start_time and end_time — accepts "HH:MM" or "HH:MM:SS".
        # Python's time.fromisoformat accepts "HH:MM" and "HH:MM:SS" directly;
        # we fall back to appending ":00" for bare "HH:MM" if needed.
        try:
            start_time = dt_time.fromisoformat(payload.start_time)
        except ValueError:
            try:
                start_time = dt_time.fromisoformat(payload.start_time + ":00")
            except ValueError:
                return JSONResponse({"error": "Invalid start_time format. Use HH:MM or HH:MM:SS."}, status_code=400)

        try:
            end_time = dt_time.fromisoformat(payload.end_time)
        except ValueError:
            try:
                end_time = dt_time.fromisoformat(payload.end_time + ":00")
            except ValueError:
                return JSONResponse({"error": "Invalid end_time format. Use HH:MM or HH:MM:SS."}, status_code=400)

        # Split overnight windows (start > end) into two same-day segments.
        dow = payload.day_of_week
        if start_time < end_time:
            segments = [(dow, start_time, end_time)]
        elif start_time == end_time:
            segments = []  # zero-length, skip
        else:
            # Crossover: split into [start, 23:59:59] + [00:00:00, end]
            segments = [
                (dow, start_time, dt_time(23, 59, 59)),
                ((dow + 1) % 7, dt_time(0, 0, 0), end_time),
            ]

        # Insert each segment. ON CONFLICT DO NOTHING to avoid duplicates.
        inserted = []
        for seg_dow, seg_start, seg_end in segments:
            try:
                row = await conn.fetchrow(
                    """INSERT INTO staff_availability (staff_id, day_of_week, start_time, end_time)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (staff_id, day_of_week, start_time, end_time) DO NOTHING
                       RETURNING id, staff_id, day_of_week::integer as day_of_week, start_time::text, end_time::text""",
                    id, seg_dow, seg_start, seg_end,
                )
                if row is not None:
                    inserted.append({
                        "id": row["id"],
                        "staff_id": row["staff_id"],
                        "day_of_week": row["day_of_week"],
                        "start_time": row["start_time"],
                        "end_time": row["end_time"],
                    })
            except Exception as e:
                print(f"Add availability (segment insert) error: {e}")
                return JSONResponse({"error": "Internal server error"}, status_code=500)

        return JSONResponse({"availability": inserted}, status_code=201)


# POST /{id}/geocode — geocode an address and update a staff member's coordinates
@router.post("/{id}/geocode")
async def geocode_staff(request: Request, id: int, payload: GeocodeStaffPayload):
    _, _, _, org_id = require_auth(request)

    if not payload.address or not payload.address.strip():
        return JSONResponse({"error": "address is required"}, status_code=400)

    # Geocode
    coords = await geocode_address(payload.address)
    if coords is None:
        return JSONResponse({"error": "Could not geocode address"}, status_code=422)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify staff belongs to org
        existing = await conn.fetchval(
            "SELECT id FROM staff WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL",
            id, org_id,
        )
        if existing is None:
            return JSONResponse({"error": "Staff not found"}, status_code=404)

        # Update coordinates
        try:
            await conn.execute(
                "UPDATE staff SET latitude = $1::float8, longitude = $2::float8, updated_at = NOW() WHERE id = $3 AND organization_id = $4",
                coords.latitude, coords.longitude, id, org_id,
            )
        except Exception as e:
            print(f"Geocode staff update error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        # Fetch updated staff
        try:
            row = await conn.fetchrow(
                """SELECT s.id, s.name, s.email, s.phone, s.address, COALESCE(s.latitude, 0)::double precision AS latitude, COALESCE(s.longitude, 0)::double precision AS longitude, s.timezone, s.organization_id, COALESCE(certs.c, '[]'::json) as certifications, COALESCE(avail.a, '[]'::json) as availability
             FROM staff s
             LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sc.id, 'name', sc.name, 'expires_at', sc.expires_at)) as c FROM staff_certifications sc WHERE sc.staff_id = s.id) certs ON true
             LEFT JOIN LATERAL (SELECT json_agg(json_build_object('id', sa.id, 'day_of_week', sa.day_of_week, 'start_time', sa.start_time::text, 'end_time', sa.end_time::text)) as a FROM staff_availability sa WHERE sa.staff_id = s.id) avail ON true
             WHERE s.id = $1 AND s.organization_id = $2 AND s.deleted_at IS NULL""",
                id, org_id,
            )
        except Exception as e:
            print(f"Geocode staff fetch error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

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
            "skills": None,  # matches Rust (query doesn't select skills column)
            "timezone": row["timezone"],
            "organization_id": row["organization_id"],
            "certifications": _parse_json_col(row["certifications"]),
            "availability": _parse_json_col(row["availability"]),
        }


# DELETE /{id} — soft-delete a staff member.
# Sets `deleted_at = NOW()` instead of removing the row.
# Blocked if the staff member has active (confirmed) assignments.
@router.delete("/{id}")
async def delete_staff(request: Request, id: int):
    user_id, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify staff exists, belongs to org, and is not already soft-deleted
        row = await conn.fetchrow(
            "SELECT id, deleted_at FROM staff WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if row is None:
            return JSONResponse({"error": "Staff not found"}, status_code=404)
        if row["deleted_at"] is not None:
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
            return JSONResponse({
                "error": f"Cannot delete staff — {assignment_count} active assignment(s). Remove or reassign first."
            }, status_code=409)

        # Soft-delete: set deleted_at = NOW()
        # Use simple_query to avoid Neon pooler Option type issues
        sql = f"UPDATE staff SET deleted_at = NOW(), updated_at = NOW() WHERE id = {id} AND organization_id = {org_id}"
        try:
            await conn.execute(sql)
        except Exception as e:
            print(f"Soft-delete staff error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        before = {"id": id, "deleted_at": None}
        after = {"id": id, "deleted_at": "now"}
        await audit_log(conn, org_id, user_id, "staff.delete", "staff", id, before, after)
        print(f"Soft-deleted staff id={id} org_id={org_id}")
        return {"ok": True, "message": "Staff member archived"}