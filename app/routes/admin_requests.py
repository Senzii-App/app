"""Admin staffing request routes — list, convert to shift, cancel.
Direct port of src/routes/admin_requests.rs.

All routes require admin auth (require_auth). org_id comes from the session.
Mounted under /api/admin prefix in main.py.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db.audit_log import log as audit_log

router = APIRouter()


# ── Query models ──────────────────────────────────────────────────────────────

class ListRequestsQuery(BaseModel):
    status: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_skills(skills_val) -> list[str]:
    """Parse required_skills JSON array into a list of strings."""
    if skills_val is None:
        return []
    if isinstance(skills_val, list):
        return [s for s in skills_val if isinstance(s, str)]
    if isinstance(skills_val, str):
        import json
        try:
            arr = json.loads(skills_val)
            if isinstance(arr, list):
                return [s for s in arr if isinstance(s, str)]
        except Exception:
            pass
    return []


async def _list_staffing_requests(conn, org_id: int, status: str | None = None) -> list[dict]:
    """List staffing requests for an org, optionally filtered by status.

    Direct port of db/staffing_requests.rs::list_staffing_requests.
    """
    sql = (
        "SELECT sr.id, sr.organization_id, sr.client_id, sr.site_id, "
        "sr.shift_date::text, sr.start_time::text, sr.end_time::text, "
        "sr.required_skills, sr.min_staff, sr.notes, sr.status, "
        "sr.created_at::text, sr.updated_at::text, "
        "c.name as client_name, c.email as client_email, ws.name as site_name "
        "FROM staffing_requests sr "
        "JOIN clients c ON c.id = sr.client_id "
        "LEFT JOIN work_sites ws ON ws.id = sr.site_id "
        "WHERE sr.organization_id = $1"
    )
    params = [org_id]
    if status is not None and status:
        sql += " AND sr.status = $2"
        params.append(status)
    sql += " ORDER BY sr.created_at DESC"

    rows = await conn.fetch(sql, *params)
    result = []
    for r in rows:
        result.append(
            {
                "id": r["id"],
                "organization_id": r["organization_id"],
                "client_id": r["client_id"],
                "site_id": r["site_id"],
                "shift_date": r["shift_date"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "required_skills": _parse_skills(r["required_skills"]),
                "min_staff": r["min_staff"],
                "notes": r["notes"],
                "status": r["status"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "client_name": r["client_name"],
                "client_email": r["client_email"],
                "site_name": r["site_name"],
            }
        )
    return result


async def _get_staffing_request_by_id(conn, request_id: int, org_id: int) -> dict | None:
    """Get a single staffing request by ID, scoped to org.

    Direct port of db/staffing_requests.rs::get_staffing_request_by_id.
    """
    row = await conn.fetchrow(
        "SELECT sr.id, sr.organization_id, sr.client_id, sr.site_id, "
        "sr.shift_date::text, sr.start_time::text, sr.end_time::text, "
        "sr.required_skills, sr.min_staff, sr.notes, sr.status, "
        "sr.created_at::text, sr.updated_at::text, "
        "c.name as client_name, c.email as client_email, ws.name as site_name "
        "FROM staffing_requests sr "
        "JOIN clients c ON c.id = sr.client_id "
        "LEFT JOIN work_sites ws ON ws.id = sr.site_id "
        "WHERE sr.id = $1 AND sr.organization_id = $2",
        request_id, org_id,
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "organization_id": row["organization_id"],
        "client_id": row["client_id"],
        "site_id": row["site_id"],
        "shift_date": row["shift_date"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "required_skills": _parse_skills(row["required_skills"]),
        "min_staff": row["min_staff"],
        "notes": row["notes"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "client_name": row["client_name"],
        "client_email": row["client_email"],
        "site_name": row["site_name"],
    }


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.get("/requests")
async def list_requests(request: Request, status: str | None = None):
    """GET /requests — list all staffing requests (optionally filter by status).

    Mirrors Rust admin_requests.rs::list_requests.
    """
    try:
        _user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            requests = await _list_staffing_requests(conn, org_id, status)
        except Exception as e:
            print(f"List requests error: {e}")
            return JSONResponse({"error": "Failed to fetch requests"}, status_code=500)

    return requests


@router.post("/requests/{id}/convert")
async def convert_request(request: Request, id: int):
    """POST /requests/{id}/convert — convert a staffing request to a shift.

    Mirrors Rust admin_requests.rs::convert_request.
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Fetch the request
        try:
            req = await _get_staffing_request_by_id(conn, id, org_id)
        except Exception as e:
            print(f"Fetch request error: {e}")
            return JSONResponse({"error": "Database error"}, status_code=500)

        if req is None:
            return JSONResponse({"error": "Staffing request not found"}, status_code=404)

        # Status guard — prevent double conversion (double-click / impatient refresh)
        if req["status"] != "open":
            return JSONResponse(
                {"error": "This request has already been converted or is no longer open"},
                status_code=409,
            )

        site_id = req["site_id"]
        if site_id is None:
            return JSONResponse(
                {"error": "Cannot convert: no work site selected"},
                status_code=400,
            )

        # Build combined start/end datetime — parse into proper datetime
        # so asyncpg sends timestamptz parameters instead of strings.
        shift_start_str = f"{req['shift_date']}T{req['start_time']}"
        shift_end_str = f"{req['shift_date']}T{req['end_time']}"
        try:
            # Try full ISO timestamp first, then naive
            shift_start = datetime.fromisoformat(shift_start_str).replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"Failed to parse shift_start '{shift_start_str}': {e}")
            return JSONResponse({"error": "Invalid start time format"}, status_code=400)
        try:
            shift_end = datetime.fromisoformat(shift_end_str).replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"Failed to parse shift_end '{shift_end_str}': {e}")
            return JSONResponse({"error": "Invalid end time format"}, status_code=400)

        import json
        skills = json.dumps(req["required_skills"]) if req["required_skills"] else json.dumps([])
        min_staff = req["min_staff"]

        # Create shift, linked back to the request via request_id
        try:
            shift_row = await conn.fetchrow(
                """INSERT INTO shifts (site_id, start_time, end_time, required_skills, min_staff, organization_id, request_id)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                   RETURNING id, site_id, start_time, end_time, required_skills, min_staff, organization_id, request_id""",
                site_id, shift_start, shift_end, skills, min_staff, org_id, id,
            )
        except Exception as e:
            print(f"Convert request error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        shift_id = shift_row["id"]

        # Mark request as 'accepted' (not 'filled' — that happens when
        # confirmed assignments reach min_staff).
        await conn.execute(
            "UPDATE staffing_requests SET status = 'accepted', updated_at = NOW() WHERE id = $1",
            id,
        )

        # Check if the shift is already fully confirmed (unlikely at
        # convert time, but keeps the invariant: if confirmed_count >=
        # min_staff, mark the request 'filled').
        try:
            await conn.execute(
                "SELECT check_request_filled(s) FROM shifts s WHERE s.id = $1",
                shift_id,
            )
        except Exception:
            pass

        shift = {
            "id": shift_id,
            "site_id": shift_row["site_id"],
            "start_time": shift_row["start_time"],
            "end_time": shift_row["end_time"],
            "required_skills": shift_row["required_skills"],
            "min_staff": shift_row["min_staff"],
            "organization_id": shift_row["organization_id"],
            "request_id": shift_row["request_id"],
        }

        before = {"id": id, "status": req["status"]}
        await audit_log(conn, org_id, user_id, "staffing_request.convert", "staffing_request", id, before, shift, None)

    return {"shift": shift, "message": "Request accepted and shift created"}


@router.post("/requests/{id}/cancel")
async def cancel_request(request: Request, id: int):
    """POST /requests/{id}/cancel — cancel a staffing request (open or accepted).

    If the request was "accepted" (a linked shift exists), the shift is
    soft-deleted, assigned_staff_id is cleared, and all pending assignments
    are rejected so the shift disappears from the schedule board and staff
    are freed. Blocked if the linked shift has confirmed assignments — the
    admin must remove or reassign those first.

    Mirrors Rust admin_requests.rs::cancel_request.
    """
    try:
        user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Can only cancel open or accepted requests
        try:
            req = await _get_staffing_request_by_id(conn, id, org_id)
        except Exception as e:
            print(f"Fetch request error: {e}")
            return JSONResponse({"error": "Database error"}, status_code=500)

        if req is None:
            return JSONResponse({"error": "Staffing request not found"}, status_code=404)

        if req["status"] != "open" and req["status"] != "accepted":
            return JSONResponse(
                {"error": "Can only cancel open or accepted requests"},
                status_code=400,
            )

        # If the request was accepted, handle the linked shift before cancelling.
        # The shift was created from this request and has request_id = $id.
        if req["status"] == "accepted":
            # Check for confirmed assignments on the linked shift
            confirmed_count = 0
            try:
                confirmed_count = await conn.fetchval(
                    """SELECT COUNT(*) FROM assignments a
                       JOIN shifts s ON s.id = a.shift_id
                       WHERE s.request_id = $1 AND s.organization_id = $2
                         AND s.deleted_at IS NULL AND a.status = 'confirmed'""",
                    id, org_id,
                )
            except Exception as e:
                print(f"Confirmed assignment count query for request {id}: {e}")
                return JSONResponse({"error": "Database error"}, status_code=500)

            if confirmed_count and confirmed_count > 0:
                return JSONResponse(
                    {"error": f"Cannot cancel — linked shift has {confirmed_count} confirmed assignment(s). Remove or reassign staff first."},
                    status_code=409,
                )

            # Soft-delete the linked shift(s), clear assigned_staff_id, and reject
            # all pending assignments so staff are freed and the shift vanishes
            # from the schedule board. Use simple_query-style SQL to avoid Neon
            # pooler Option type issues.
            soft_delete_sql = (
                f"UPDATE shifts SET deleted_at = NOW(), assigned_staff_id = NULL, updated_at = NOW() "
                f"WHERE request_id = {id} AND organization_id = {org_id} AND deleted_at IS NULL"
            )
            try:
                await conn.execute(soft_delete_sql)
            except Exception as e:
                print(f"Failed to soft-delete shift for cancelled request {id}: {e}")
                return JSONResponse({"error": "Database error"}, status_code=500)

            # Reject all pending assignments on those shifts
            reject_sql = (
                f"UPDATE assignments SET status = 'rejected', updated_at = NOW() "
                f"WHERE shift_id IN (SELECT id FROM shifts WHERE request_id = {id} AND organization_id = {org_id}) "
                f"AND status = 'pending'"
            )
            try:
                await conn.execute(reject_sql)
            except Exception as e:
                print(f"Failed to reject pending assignments for cancelled request {id}: {e}")

            print(f"Soft-deleted linked shift(s) for cancelled request id={id} org_id={org_id}")

        # Now mark the request as cancelled
        try:
            row = await conn.fetchrow(
                "UPDATE staffing_requests SET status = 'cancelled', updated_at = NOW() "
                "WHERE id = $1 AND organization_id = $2 RETURNING id, status",
                id, org_id,
            )
        except Exception as e:
            print(f"Cancel request error: {e}")
            return JSONResponse({"error": "Database error"}, status_code=500)

        if row is None:
            return JSONResponse({"error": "Request not found"}, status_code=404)

        before = {"id": id, "status": req["status"]}
        after = {"id": row["id"], "status": row["status"]}
        await audit_log(conn, org_id, user_id, "staffing_request.cancel", "staffing_request", id, before, after, None)

    return {"id": row["id"], "status": row["status"], "message": "Request cancelled"}