"""Staff portal routes — magic link + password login, profile, certifications,
availability, shifts, and accept/decline assignments.

Direct port of src/routes/staff_portal.rs from the Rust project.
All SQL queries are copied 1:1 from the Rust source.

Router has no prefix — it's set in main.py as '/staff/api'.
"""
import uuid
from datetime import datetime, time, timezone

import bcrypt
from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db.pool import get_pool
from app.middleware.auth import (
    require_staff_session,
    save_session,
    destroy_session,
    STAFF_ID_KEY,
    SESSION_START_KEY,
    LAST_ACTIVITY_KEY,
)
from app.services.email import send_shift_confirmation_email
from app.services.geocode import geocode_address

router = APIRouter()


# ── Pydantic request models ──────────────────────────────────────────────────


class PasswordLoginPayload(BaseModel):
    email: str
    password: str


class SetPasswordPayload(BaseModel):
    password: str


class UpdateProfilePayload(BaseModel):
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    timezone: str | None = None


class AddCertificationPayload(BaseModel):
    name: str
    expires_at: str | None = None


class AvailabilityWindow(BaseModel):
    day_of_week: int
    start_time: str
    end_time: str


class AvailabilityPayload(BaseModel):
    windows: list[AvailabilityWindow]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_time(s: str) -> time:
    """Parse 'HH:MM' or 'HH:MM:SS' into a datetime.time."""
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time format: {s}")


def _parse_expires_at(s: str) -> datetime | None:
    """Parse expires_at string into an aware datetime.

    Accepts both date-only ('YYYY-MM-DD') and full ISO timestamps
    ('YYYY-MM-DDTHH:MM:SSZ' / with offset).
    """
    if not s:
        return None
    # Try full ISO 8601 with offset (e.g. 2027-06-15T00:00:00Z, 2027-06-15T00:00:00+00:00)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    # Try date-only YYYY-MM-DD -> midnight UTC
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("Invalid expires_at format")


async def _fetch_staff_profile(conn, staff_id: int) -> dict | None:
    """Fetch full staff profile with certifications and availability.

    Mirrors the LATERAL JOIN query used by get_me / post_login / set-password
    in the Rust source.
    """
    row = await conn.fetchrow(
        """
        SELECT s.id, s.name, s.email, s.phone, s.address,
               s.latitude::double precision AS latitude,
               s.longitude::double precision AS longitude,
               s.timezone,
               COALESCE(cert.certs, '[]'::json) AS certifications,
               COALESCE(avail.windows, '[]'::json) AS availability,
               (u.password_hash IS NOT NULL) AS has_password
        FROM staff s
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN LATERAL (
          SELECT json_agg(json_build_object('id', sc.id, 'name', sc.name, 'expires_at', sc.expires_at)) AS certs
          FROM staff_certifications sc WHERE sc.staff_id = s.id
        ) cert ON true
        LEFT JOIN LATERAL (
          SELECT json_agg(json_build_object(
            'id', sa.id, 'day_of_week', sa.day_of_week,
            'start_time', sa.start_time::text, 'end_time', sa.end_time::text
          ) ORDER BY sa.day_of_week, sa.start_time) AS windows
          FROM staff_availability sa WHERE sa.staff_id = s.id
        ) avail ON true
        WHERE s.id = $1 AND s.deleted_at IS NULL
        """,
        staff_id,
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "address": row["address"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
        "certifications": row["certifications"],
        "availability": row["availability"],
        "has_password": row["has_password"],
    }


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
    """GET /login?token=... — magic link login for staff."""
    if not token:
        raise HTTPException(status_code=400, detail="Token required")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify magic token (JOIN staff table for name/email/org_id)
        row = await conn.fetchrow(
            """
            SELECT mt.staff_id, s.email AS staff_email, s.name AS staff_name, s.organization_id
            FROM magic_tokens mt
            JOIN staff s ON s.id = mt.staff_id
            WHERE mt.token = $1 AND mt.used_at IS NULL AND mt.expires_at > NOW() AND s.deleted_at IS NULL
            """,
            token,
        )
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid or expired link")

        staff_id = row["staff_id"]
        staff_email = row["staff_email"]
        staff_name = row["staff_name"]
        oid = row["organization_id"] or 0

        # Consume token
        await conn.execute("DELETE FROM magic_tokens WHERE token = $1", token)

        # Get or create user linked to this staff record
        user_row = await conn.fetchrow(
            "SELECT id, email, password_hash, organization_id FROM users WHERE email = $1 AND role = 'staff'",
            staff_email,
        )
        if user_row is not None:
            user_id = user_row["id"]
            has_password = bool(user_row["password_hash"])
        else:
            # Create user for this staff member
            temp_password = str(uuid.uuid4())
            pw_hash = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()
            try:
                user_id = await conn.fetchval(
                    "INSERT INTO users (email, password_hash, role, organization_id) VALUES ($1, $2, 'staff', $3) RETURNING id",
                    staff_email,
                    pw_hash,
                    oid,
                )
            except Exception:
                raise HTTPException(status_code=500, detail="Failed to create user")
            # Link user to staff record
            await conn.execute("UPDATE staff SET user_id = $1 WHERE id = $2", user_id, staff_id)
            has_password = False

    # Save session
    session = request.session
    now = int(datetime.now(timezone.utc).timestamp())
    save_session(request, user_id, staff_email, "staff", oid)
    session[STAFF_ID_KEY] = staff_id
    session[SESSION_START_KEY] = now
    session[LAST_ACTIVITY_KEY] = now

    return {
        "ok": True,
        "staff": {"id": staff_id, "name": staff_name, "email": staff_email},
        "hasPassword": has_password,
    }


@router.post("/login")
async def post_login_password(request: Request, payload: PasswordLoginPayload):
    """POST /login — email + password login for staff."""
    email = payload.email.strip().lower()
    password = payload.password

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.email, u.password_hash, u.role, u.organization_id, s.id AS staff_id
            FROM users u
            JOIN staff s ON s.user_id = u.id
            WHERE u.email = $1 AND u.role = 'staff' AND s.deleted_at IS NULL
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
        staff_id = row["staff_id"]
        org_id = row["organization_id"] or 0

        # Save session
        session = request.session
        now = int(datetime.now(timezone.utc).timestamp())
        save_session(request, uid, uemail, "staff", org_id)
        session[STAFF_ID_KEY] = staff_id
        session[SESSION_START_KEY] = now
        session[LAST_ACTIVITY_KEY] = now

        # Fetch full staff profile to return with login response
        profile = await _fetch_staff_profile(conn, staff_id)
        if profile is None:
            profile = {
                "id": staff_id,
                "name": uemail,
                "email": uemail,
                "has_password": True,
            }

    return {"ok": True, "hasPassword": True, "profile": profile}


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
            """
            SELECT mt.staff_id, mt.expires_at
            FROM magic_tokens mt
            JOIN staff s ON s.id = mt.staff_id
            WHERE mt.token = $1 AND mt.used_at IS NULL AND mt.expires_at > NOW() AND s.deleted_at IS NULL
            """,
            token,
        )
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired link")

    return {"ok": True, "valid": True}


@router.post("/set-password")
async def post_set_password(request: Request, payload: SetPasswordPayload):
    """POST /set-password — set password for staff."""
    user_id, _org_id, staff_id = require_staff_session(request)

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
        # Fetch profile to return alongside the success response
        profile = await _fetch_staff_profile(conn, staff_id)

    resp = {"ok": True}
    if profile is not None:
        resp["profile"] = profile
    return resp


@router.get("/logout")
async def get_logout(request: Request):
    """GET /logout — destroy the staff session."""
    destroy_session(request)
    return {"ok": True}


@router.post("/logout")
async def post_logout(request: Request):
    """POST /logout — destroy the staff session (alias)."""
    destroy_session(request)
    return {"ok": True}


@router.get("/me")
async def get_me(request: Request):
    """GET /me — get staff profile."""
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        profile = await _fetch_staff_profile(conn, staff_id)

    if profile is None:
        raise HTTPException(status_code=404, detail="Staff not found")
    return profile


@router.patch("/me")
async def patch_me(request: Request, payload: UpdateProfilePayload):
    """PATCH /me — update staff profile (name, phone, address, timezone).

    If address is provided and non-empty, it is geocoded via Nominatim and
    latitude/longitude are stored alongside.
    """
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        updates: list[str] = []
        params: list = []
        idx = 1

        if payload.name is not None:
            updates.append(f"name = ${idx}")
            params.append(payload.name)
            idx += 1
        if payload.phone is not None:
            updates.append(f"phone = ${idx}")
            params.append(payload.phone)
            idx += 1
        # Geocode address if provided
        if payload.address is not None and payload.address.strip():
            updates.append(f"address = ${idx}")
            params.append(payload.address)
            idx += 1
            coords = await geocode_address(payload.address)
            if coords is not None:
                updates.append(f"latitude = ${idx}::float8")
                params.append(coords.latitude)
                idx += 1
                updates.append(f"longitude = ${idx}::float8")
                params.append(coords.longitude)
                idx += 1
            else:
                print("[staff-portal] Could not geocode address during profile update")
        if payload.timezone is not None:
            updates.append(f"timezone = ${idx}")
            params.append(payload.timezone)
            idx += 1

        if not updates:
            raise HTTPException(status_code=400, detail="Nothing to update")

        updates.append("updated_at = NOW()")
        params.append(staff_id)
        sql = (
            f"UPDATE staff SET {', '.join(updates)} WHERE id = ${idx} "
            "RETURNING id, name, email, phone, address, "
            "latitude::double precision AS latitude, "
            "longitude::double precision AS longitude, timezone"
        )
        row = await conn.fetchrow(sql, *params)
        if row is None:
            raise HTTPException(status_code=500, detail="Internal server error")

    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "address": row["address"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
    }


@router.get("/shifts")
async def get_shifts(request: Request):
    """GET /shifts — get staff's assigned shifts."""
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id AS assignment_id, a.status AS assignment_status,
                   s.id, s.start_time::text AS start_time, s.end_time::text AS end_time,
                   ws.name AS site_name, ws.address AS site_address,
                   COALESCE(ws.latitude, 0)::double precision AS site_lat,
                   COALESCE(ws.longitude, 0)::double precision AS site_lng
            FROM assignments a
            JOIN shifts s ON s.id = a.shift_id
            JOIN work_sites ws ON ws.id = s.site_id
            WHERE a.staff_id = $1
            ORDER BY s.start_time
            """,
            staff_id,
        )

    return [
        {
            "assignment_id": r["assignment_id"],
            "assignment_status": r["assignment_status"],
            "id": r["id"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "site_name": r["site_name"],
            "site_address": r["site_address"],
            "site_lat": r["site_lat"],
            "site_lng": r["site_lng"],
        }
        for r in rows
    ]


@router.post("/shifts/{assignment_id}/accept")
async def accept_assignment(request: Request, assignment_id: int):
    """POST /shifts/{id}/accept — accept a pending assignment."""
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE assignments SET status = 'confirmed', confirmed_at = NOW()
            WHERE id = $1 AND staff_id = $2 AND status = 'pending'
            RETURNING id, shift_id, staff_id, status
            """,
            assignment_id,
            staff_id,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Assignment not found or already responded",
            )

        shift_id = row["shift_id"]

        # Auto-fill check: if this shift is linked to a staffing_request and
        # confirmed assignments now meet min_staff, mark the request 'filled'.
        await conn.execute("SELECT check_request_filled(s) FROM shifts s WHERE s.id = $1", shift_id)

        # Send shift confirmation email to the staff member
        details = await conn.fetchrow(
            """
            SELECT s.name AS staff_name, s.email AS staff_email, s.timezone,
                   sh.start_time, sh.end_time,
                   ws.name AS site_name,
                   o.name AS org_name
            FROM staff s
            JOIN assignments a ON a.staff_id = s.id
            JOIN shifts sh ON sh.id = a.shift_id
            JOIN work_sites ws ON ws.id = sh.site_id
            JOIN organizations o ON o.id = s.organization_id
            WHERE a.id = $1
            """,
            assignment_id,
        )
        if details is not None:
            shift_start = details["start_time"]
            shift_end = details["end_time"]
            shift_date = shift_start.strftime("%A, %B %d, %Y")
            start_fmt = shift_start.strftime("%H:%M")
            end_fmt = shift_end.strftime("%H:%M")
            email_result = await send_shift_confirmation_email(
                to=details["staff_email"],
                staff_name=details["staff_name"],
                org_name=details["org_name"],
                site_name=details["site_name"],
                shift_date=shift_date,
                start_time=start_fmt,
                end_time=end_fmt,
            )
            if not email_result.get("sent"):
                print(
                    f"[email] Shift confirmation email not sent to {details['staff_email']} "
                    f"(reason: {email_result.get('reason')})"
                )

    return {
        "id": row["id"],
        "shift_id": row["shift_id"],
        "staff_id": row["staff_id"],
        "status": row["status"],
    }


@router.post("/shifts/{assignment_id}/decline")
async def decline_assignment(request: Request, assignment_id: int):
    """POST /shifts/{id}/decline — decline a pending assignment."""
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE assignments SET status = 'rejected'
            WHERE id = $1 AND staff_id = $2 AND status = 'pending'
            RETURNING id, shift_id, staff_id, status
            """,
            assignment_id,
            staff_id,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Assignment not found or already responded",
            )

    return {
        "id": row["id"],
        "shift_id": row["shift_id"],
        "staff_id": row["staff_id"],
        "status": row["status"],
    }


@router.get("/certifications")
async def get_certifications(request: Request):
    """GET /certifications — list org certifications (for staff portal dropdown)."""
    _user_id, org_id, _staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        certs = await _list_org_certs(conn, org_id)

    return {"certifications": certs}


@router.post("/certifications")
async def add_certification(request: Request, payload: AddCertificationPayload):
    """POST /certifications — add a certification to the staff member's profile."""
    _user_id, _org_id, staff_id = require_staff_session(request)

    if not payload.name:
        raise HTTPException(status_code=400, detail="name is required")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Look up staff's org_id
        staff_org = await conn.fetchrow(
            "SELECT organization_id FROM staff WHERE id = $1 AND deleted_at IS NULL",
            staff_id,
        )
        org_id = staff_org["organization_id"] if staff_org else 0

        # Validate: cert name must exist in the org's certification list
        cert_row = await conn.fetchrow(
            "SELECT id FROM organization_certifications WHERE organization_id = $1 AND name = $2",
            org_id,
            payload.name,
        )
        if cert_row is None:
            raise HTTPException(
                status_code=400,
                detail="Unknown certification. Choose from your organization's certification list.",
            )
        cert_id = cert_row["id"]

        # Parse expires_at
        expires_at: datetime | None = None
        if payload.expires_at and payload.expires_at.strip():
            try:
                expires_at = _parse_expires_at(payload.expires_at)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid expires_at format. Use YYYY-MM-DD or ISO 8601 timestamp.",
                )

        row = await conn.fetchrow(
            """
            INSERT INTO staff_certifications (staff_id, name, expires_at, cert_id)
            VALUES ($1, $2, $3, $4)
            RETURNING id, staff_id, name, expires_at::text AS expires_at
            """,
            staff_id,
            payload.name,
            expires_at,
            cert_id,
        )

    return JSONResponse(
        status_code=201,
        content={
            "id": row["id"],
            "staff_id": row["staff_id"],
            "name": row["name"],
            "expires_at": row["expires_at"],
        },
    )


@router.delete("/certifications/{cert_id}")
async def delete_certification(request: Request, cert_id: int):
    """DELETE /certifications/{id} — delete a certification from the staff member's profile."""
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM staff_certifications WHERE id = $1 AND staff_id = $2 RETURNING id",
            cert_id,
            staff_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")

    return {"ok": True}


@router.get("/availability")
async def get_availability(request: Request):
    """GET /availability — list the staff member's availability windows."""
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, staff_id, day_of_week::integer AS day_of_week,
                   start_time::text AS start_time, end_time::text AS end_time
            FROM staff_availability
            WHERE staff_id = $1
            ORDER BY day_of_week, start_time
            """,
            staff_id,
        )

    return [
        {
            "id": r["id"],
            "staff_id": r["staff_id"],
            "day_of_week": r["day_of_week"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
        }
        for r in rows
    ]


@router.post("/availability")
async def post_availability(request: Request, payload: AvailabilityPayload):
    """POST /availability — replace all availability windows for the staff member.

    Runs in a transaction: deletes existing windows, then inserts the new ones.
    Overnight windows (start >= end) are split into two same-day segments.
    """
    _user_id, _org_id, staff_id = require_staff_session(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("DELETE FROM staff_availability WHERE staff_id = $1", staff_id)

            for w in payload.windows:
                if w.day_of_week < 0 or not w.start_time or not w.end_time:
                    continue
                try:
                    start_time = _parse_time(w.start_time)
                except ValueError:
                    await tr.rollback()
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid start_time format. Use HH:MM.",
                    )
                try:
                    end_time = _parse_time(w.end_time)
                except ValueError:
                    await tr.rollback()
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid end_time format. Use HH:MM.",
                    )

                # Split overnight windows (start >= end) into two same-day segments
                if start_time < end_time:
                    segments = [(w.day_of_week, start_time, end_time)]
                elif start_time == end_time:
                    continue  # zero-length, skip
                else:
                    segments = [
                        (w.day_of_week, start_time, time(23, 59, 59)),
                        ((w.day_of_week + 1) % 7, time(0, 0, 0), end_time),
                    ]

                for seg_dow, seg_start, seg_end in segments:
                    await conn.execute(
                        """
                        INSERT INTO staff_availability (staff_id, day_of_week, start_time, end_time)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (staff_id, day_of_week, start_time, end_time) DO NOTHING
                        """,
                        staff_id,
                        seg_dow,
                        seg_start,
                        seg_end,
                    )

            await tr.commit()
        except HTTPException:
            raise
        except Exception as e:
            await tr.rollback()
            print(f"[staff-portal] Availability transaction error: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

        # Fetch updated availability
        rows = await conn.fetch(
            """
            SELECT id, staff_id, day_of_week::integer AS day_of_week,
                   start_time::text AS start_time, end_time::text AS end_time
            FROM staff_availability
            WHERE staff_id = $1
            ORDER BY day_of_week, start_time
            """,
            staff_id,
        )

    return [
        {
            "id": r["id"],
            "staff_id": r["staff_id"],
            "day_of_week": r["day_of_week"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
        }
        for r in rows
    ]


@router.post("/geocode")
async def post_geocode(request: Request, payload: dict):
    """POST /geocode — geocode an address string.

    Request body: { "address": "123 Main St, City, ST" }
    Returns: { "latitude": float, "longitude": float } or 404 if not resolved.
    """
    address = (payload.get("address") or "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="address is required")

    coords = await geocode_address(address)
    if coords is None:
        raise HTTPException(status_code=404, detail="Could not geocode address")

    return {"latitude": coords.latitude, "longitude": coords.longitude}