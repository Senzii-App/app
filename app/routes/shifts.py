"""Shifts route — Shift CRUD + matching endpoints (org-scoped).
Direct port of src/routes/shifts.rs from the Rust/Axum project.

All routes require admin auth. org_id comes exclusively from the session via
require_auth(request). The router has no prefix — it is set in main.py
(`app.include_router(shifts.router, prefix="/api/shifts")`).
"""
from datetime import datetime, timezone, date, time
from typing import Any

from fastapi import APIRouter, Request, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db import shifts as shifts_db
from app.db import audit_log
from app.services import email as email_service


router = APIRouter()


# ── Request payloads ──────────────────────────────────────────────────────────

class CreateShiftPayload(BaseModel):
    site_id: int
    start_time: str
    end_time: str
    required_skills: list[str] | None = None
    min_staff: int | None = None


class AssignPayload(BaseModel):
    staff_id: int
    score: float | None = None


class ConfirmPayload(BaseModel):
    confirmed_by: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_timestamptz(s: str) -> datetime:
    """Parse a datetime string into an aware UTC datetime for asyncpg TIMESTAMPTZ.

    Accepts RFC 3339 ("2025-08-01T07:00:00Z" or with offset) or naive
    "YYYY-MM-DDTHH:MM:SS" / "YYYY-MM-DDTHH:MM" (assumed UTC).
    Raises ValueError on bad input.
    """
    # Try RFC 3339 (with Z or offset)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Try naive "%Y-%m-%dT%H:%M:%S" / "%Y-%m-%dT%H:%M"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            naive = datetime.strptime(s, fmt)
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Invalid datetime '{s}'")


def _parse_date_query(d: str, end_of_day: bool = False) -> datetime:
    """Parse a "YYYY-MM-DD" string into a UTC datetime.

    end_of_day=False → 00:00:00 UTC; end_of_day=True → 23:59:59 UTC.
    Raises ValueError on bad input.
    """
    nd = datetime.strptime(d, "%Y-%m-%d").date()
    t = time(23, 59, 59) if end_of_day else time(0, 0, 0)
    return datetime.combine(nd, t, tzinfo=timezone.utc)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/")
async def list_shifts(
    request: Request,
    site_id: int | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
):
    """GET / — list shifts for the org, optionally filtered by site_id and date range."""
    _, _, _, org_id = require_auth(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        query_parts = [
            "SELECT s.id, s.site_id, s.start_time::text, s.end_time::text,",
            "s.required_skills, s.min_staff, s.organization_id, s.assigned_staff_id,",
            "ws.name AS site_name, COALESCE(ca.cnt, 0) AS confirmed_count",
            "FROM shifts s",
            "JOIN work_sites ws ON ws.id = s.site_id",
            "LEFT JOIN LATERAL (SELECT COUNT(*) AS cnt FROM assignments a "
            "WHERE a.shift_id = s.id AND a.status = 'confirmed') ca ON true",
            "WHERE s.organization_id = $1 AND s.deleted_at IS NULL",
        ]
        query_str = " ".join(query_parts)
        params: list[Any] = [org_id]
        param_idx = 2

        if site_id is not None:
            query_str += f" AND s.site_id = ${param_idx}"
            params.append(site_id)
            param_idx += 1

        if from_date is not None:
            try:
                from_dt = _parse_date_query(from_date, end_of_day=False)
            except ValueError:
                return JSONResponse(
                    {"error": "Invalid from_date format, expected YYYY-MM-DD"},
                    status_code=400,
                )
            query_str += f" AND s.start_time >= ${param_idx}"
            params.append(from_dt)
            param_idx += 1

        if to_date is not None:
            try:
                to_dt = _parse_date_query(to_date, end_of_day=True)
            except ValueError:
                return JSONResponse(
                    {"error": "Invalid to_date format, expected YYYY-MM-DD"},
                    status_code=400,
                )
            query_str += f" AND s.start_time <= ${param_idx}"
            params.append(to_dt)
            param_idx += 1

        query_str += " ORDER BY s.start_time"

        rows = await conn.fetch(query_str, *params)
        shifts = [
            {
                "id": r["id"],
                "site_id": r["site_id"],
                "site_name": r["site_name"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "required_skills": r["required_skills"],
                "min_staff": r["min_staff"],
                "organization_id": r["organization_id"],
                "assigned_staff_id": r["assigned_staff_id"],
                "confirmed_count": r["confirmed_count"],
            }
            for r in rows
        ]
        return {"shifts": shifts}


@router.post("/")
async def create_shift(request: Request, payload: CreateShiftPayload):
    """POST / — create a shift."""
    user_id, _, _, org_id = require_auth(request)

    if payload.site_id == 0 or not payload.start_time or not payload.end_time:
        return JSONResponse(
            {"error": "site_id, start_time, end_time are required"},
            status_code=400,
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify site belongs to this org
        site_row = await conn.fetchrow(
            "SELECT id FROM work_sites WHERE id = $1 AND organization_id = $2",
            payload.site_id, org_id,
        )
        if site_row is None:
            return JSONResponse(
                {"error": "Work site not found or belongs to another organization"},
                status_code=404,
            )

        # required_skills: JSONB — asyncpg accepts a list and will encode it
        required_skills = payload.required_skills if payload.required_skills is not None else []
        min_staff = payload.min_staff if payload.min_staff is not None else 1

        try:
            shift_start = _parse_timestamptz(payload.start_time)
        except ValueError as e:
            return JSONResponse({"error": "Invalid start_time format"}, status_code=400)
        try:
            shift_end = _parse_timestamptz(payload.end_time)
        except ValueError:
            return JSONResponse({"error": "Invalid end_time format"}, status_code=400)

        try:
            row = await conn.fetchrow(
                """INSERT INTO shifts (site_id, start_time, end_time, required_skills, min_staff, organization_id)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                   RETURNING id, site_id, start_time::text, end_time::text,
                             required_skills, min_staff, organization_id""",
                payload.site_id, shift_start, shift_end, required_skills, min_staff, org_id,
            )
        except Exception:
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        shift_id = row["id"]
        shift = {
            "id": shift_id,
            "site_id": row["site_id"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "required_skills": row["required_skills"],
            "min_staff": row["min_staff"],
            "organization_id": row["organization_id"],
        }
        await audit_log.log(
            conn, org_id, user_id, "shift.create", "shift",
            shift_id, None, shift, None,
        )
        return JSONResponse(shift, status_code=201)


@router.delete("/{id}")
async def delete_shift(request: Request, id: int = Path(...)):
    """DELETE /{id} — soft-delete a shift.

    Sets deleted_at = NOW(), clears assigned_staff_id, rejects pending assignments.
    Blocked if the shift has confirmed assignments.
    """
    user_id, _, _, org_id = require_auth(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify shift exists, belongs to org, not already soft-deleted
        existing = await conn.fetchrow(
            "SELECT id, deleted_at FROM shifts WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if existing is None:
            return JSONResponse({"error": "Shift not found"}, status_code=404)
        if existing["deleted_at"] is not None:
            return JSONResponse({"error": "Shift already deleted"}, status_code=409)

        # Block if confirmed assignments exist
        confirmed_count = await conn.fetchval(
            "SELECT COUNT(*) FROM assignments WHERE shift_id = $1 AND organization_id = $2 AND status = 'confirmed'",
            id, org_id,
        )
        if confirmed_count > 0:
            return JSONResponse(
                {"error": f"Cannot delete shift — {confirmed_count} confirmed assignment(s). "
                          "Remove or reassign staff first."},
                status_code=409,
            )

        # Soft-delete using simple_query (avoids Neon pooler Option type issues,
        # matching the Rust implementation).
        sql = (f"UPDATE shifts SET deleted_at = NOW(), assigned_staff_id = NULL, "
               f"updated_at = NOW() WHERE id = {id} AND organization_id = {org_id}")
        await conn.execute(sql)

        # Reject all pending assignments for this shift
        reject_sql = (f"UPDATE assignments SET status = 'rejected', updated_at = NOW() "
                      f"WHERE shift_id = {id} AND organization_id = {org_id} AND status = 'pending'")
        try:
            await conn.execute(reject_sql)
        except Exception:
            pass  # log only, matching Rust's tracing::warn

        before = {"id": id, "deleted_at": None}
        after = {"id": id, "deleted_at": "now", "assigned_staff_id": None}
        await audit_log.log(
            conn, org_id, user_id, "shift.delete", "shift",
            id, before, after, None,
        )
        return {"ok": True, "message": "Shift deleted"}


@router.get("/{id}/match")
async def match_staff(request: Request, id: int = Path(...)):
    """GET /{id}/match — ranked staff candidates with scores, gap reasons, and diagnosis."""
    _, _, _, org_id = require_auth(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Run the real matching engine — returns ALL staff (available + unavailable)
        all_candidates = await shifts_db.find_matching_staff(conn, id, org_id, 0.3, 0.4, 0.3)
        if all_candidates is None:
            return JSONResponse({"error": "Shift not found"}, status_code=404)

        # 1b. Exclude staff already assigned to THIS shift (non-rejected)
        assigned_rows = await conn.fetch(
            "SELECT a.staff_id FROM assignments a "
            "WHERE a.shift_id = $1 AND a.organization_id = $2 AND a.status != 'rejected'",
            id, org_id,
        )
        assigned_staff = {r["staff_id"] for r in assigned_rows}
        all_candidates = [c for c in all_candidates if c["staff_id"] not in assigned_staff]

        # 2. Get shift details for conflict detection and gap reasons
        shift_row = await conn.fetchrow(
            "SELECT s.id, s.start_time, s.end_time, s.required_skills, s.min_staff, "
            "ws.name AS site_name, ws.latitude::double precision AS site_lat, "
            "ws.longitude::double precision AS site_lng "
            "FROM shifts s "
            "JOIN work_sites ws ON ws.id = s.site_id "
            "WHERE s.id = $1 AND s.organization_id = $2 AND s.deleted_at IS NULL",
            id, org_id,
        )
        if shift_row is None:
            return JSONResponse({"error": "Shift not found"}, status_code=404)

        shift_start = shift_row["start_time"]
        shift_end = shift_row["end_time"]
        shift_skills_json = shift_row["required_skills"]
        if shift_skills_json:
            shift_skills = [s for s in shift_skills_json if isinstance(s, str)]
        else:
            shift_skills = []
        site_lat = shift_row["site_lat"]
        site_lng = shift_row["site_lng"]

        # 3. Fetch overlapping assignments (exclude this shift) for conflict detection
        conflict_rows = await conn.fetch(
            "SELECT a.staff_id, a.status, sh.start_time::text, sh.end_time::text, ws2.name AS conflict_site "
            "FROM assignments a "
            "JOIN shifts sh ON sh.id = a.shift_id "
            "JOIN work_sites ws2 ON ws2.id = sh.site_id "
            "WHERE a.organization_id = $1 "
            "  AND a.status != 'rejected' "
            "  AND a.shift_id != $4 "
            "  AND sh.start_time < $3 "
            "  AND sh.end_time > $2",
            org_id, shift_start, shift_end, id,
        )

        # Build map: staff_id → list of (status, cstart, csite)
        conflict_map: dict[int, list[tuple[str, str, str]]] = {}
        for row in conflict_rows:
            sid = row["staff_id"]
            conflict_map.setdefault(sid, []).append(
                (row["status"], row["start_time"], row["conflict_site"])
            )

        # 4. Count total org staff for diagnosis
        total_staff = await conn.fetchval(
            "SELECT COUNT(*) FROM staff WHERE organization_id = $1 AND deleted_at IS NULL",
            org_id,
        )

        # 5. Enrich each candidate with conflict info and gap reasons
        result = []
        diag_available_but_conflict = 0
        diag_available_but_far = 0
        diag_not_available = 0
        diag_missing_skills = 0
        diag_no_location = 0

        for c in all_candidates:
            gap_reasons = []
            has_conflict = False
            conflict_details = None

            is_available = c["breakdown"]["availability_score"] > 0.0

            # Conflict detection
            conflicts = conflict_map.get(c["staff_id"])
            if conflicts:
                has_conflict = True
                first = conflicts[0]
                conflict_details = f"Already assigned to overlapping shift at {first[2]}"
                gap_reasons.append({
                    "kind": "conflict",
                    "message": f"Already assigned to overlapping shift at {first[2]} ({first[1]} — {first[0]})",
                })
                if is_available:
                    diag_available_but_conflict += 1

            # Availability gap
            if c["breakdown"]["availability_score"] == 0.0:
                diag_not_available += 1
                gap_reasons.append({
                    "kind": "no_availability",
                    "message": "Not available during this shift's day/time",
                })

            # Skills gap — list missing skills
            if c["breakdown"]["skills_score"] < 1.0:
                if is_available:
                    diag_missing_skills += 1
                missing = [
                    s for s in shift_skills
                    if not any(cs.lower() == s.lower() for cs in c["certifications"])
                ]
                if missing:
                    gap_reasons.append({
                        "kind": "missing_skills",
                        "message": f"Missing: {', '.join(missing)}",
                    })

            # Proximity gap
            if c["breakdown"]["proximity_score"] == 0.0:
                lat = c["staff_location"]["latitude"]
                lng = c["staff_location"]["longitude"]
                if lat is not None and lng is not None:
                    if is_available:
                        diag_available_but_far += 1
                    gap_reasons.append({
                        "kind": "far_away",
                        "message": "Located more than 50km away from this site",
                    })
                else:
                    diag_no_location += 1
                    gap_reasons.append({
                        "kind": "no_location",
                        "message": "No location on file — cannot assess proximity",
                    })
            elif c["breakdown"]["proximity_score"] < 0.7:
                lat = c["staff_location"]["latitude"]
                lng = c["staff_location"]["longitude"]
                if lat is not None and lng is not None:
                    if site_lat is not None and site_lng is not None:
                        dist_km = shifts_db.haversine_km(lat, lng, site_lat, site_lng)
                    else:
                        dist_km = 0.0
                    dist_mi = round(dist_km * 0.621371)
                    gap_reasons.append({
                        "kind": "far_away",
                        "message": f"Available but {dist_mi}mi away from this site",
                    })
                    if is_available:
                        diag_available_but_far += 1

            result.append({
                "staff_id": c["staff_id"],
                "name": c["name"],
                "email": c["email"],
                "score": c["score"],
                "breakdown": c["breakdown"],
                "staff_location": c["staff_location"],
                "shift_local_time": c["shift_local_time"],
                "certifications": c["certifications"],
                "has_conflict": has_conflict,
                "conflict_details": conflict_details,
                "gap_reasons": gap_reasons,
            })

        # Split into available (in list) and unavailable (counted in diagnosis)
        available_candidates = []
        unavailable_count = 0
        for c in result:
            if c["breakdown"]["availability_score"] > 0.0:
                available_candidates.append(c)
            else:
                unavailable_count += 1

        # 6. Build diagnosis summary
        def _plural(n: int, one: str, many: str) -> str:
            return one if n == 1 else many

        summary_parts = []
        if diag_not_available > 0:
            summary_parts.append(
                f"{diag_not_available} staff member{_plural(diag_not_available, ' is', 's are')} "
                "not available during this shift"
            )
        if diag_available_but_conflict > 0:
            summary_parts.append(
                f"{diag_available_but_conflict} staff member"
                f"{_plural(diag_available_but_conflict, ' is', 's are')} "
                "available but already assigned to overlapping shifts"
            )
        if diag_available_but_far > 0:
            summary_parts.append(
                f"{diag_available_but_far} available staff member"
                f"{_plural(diag_available_but_far, ' is', 's are')} "
                "far from this site"
            )
        if diag_missing_skills > 0:
            summary_parts.append(
                f"{diag_missing_skills} staff member"
                f"{_plural(diag_missing_skills, ' is', 's are')} "
                "missing required skills"
            )
        if diag_no_location > 0:
            summary_parts.append(
                f"{diag_no_location} staff member"
                f"{_plural(diag_no_location, ' has', 's have')} "
                "no location on file"
            )
        if not summary_parts and unavailable_count > 0:
            summary_parts.append(f"{unavailable_count} staff members found, none fully matched")

        diagnosis = {
            "total_org_staff": total_staff,
            "available_but_conflict": diag_available_but_conflict,
            "available_but_far": diag_available_but_far,
            "not_available": diag_not_available,
            "missing_skills": diag_missing_skills,
            "no_location": diag_no_location,
            "summary": (
                "All staff matched successfully."
                if not summary_parts
                else ". ".join(summary_parts) + "."
            ),
        }

        return {
            "shift_id": id,
            "candidates": available_candidates,
            "diagnosis": diagnosis,
        }


@router.post("/{id}/assign")
async def assign_staff(request: Request, payload: AssignPayload, id: int = Path(...)):
    """POST /{id}/assign — create a pending assignment."""
    user_id, _, _, org_id = require_auth(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify shift exists and get its time range
        shift_row = await conn.fetchrow(
            "SELECT id, start_time, end_time FROM shifts "
            "WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL",
            id, org_id,
        )
        if shift_row is None:
            return JSONResponse({"error": "Shift not found"}, status_code=404)
        shift_start = shift_row["start_time"]
        shift_end = shift_row["end_time"]

        # Check for overlapping assignments — can't be in two places at once
        overlap_rows = await conn.fetch(
            "SELECT a.id, sh.start_time::text, sh.end_time::text, ws.name AS site_name "
            "FROM assignments a "
            "JOIN shifts sh ON sh.id = a.shift_id "
            "JOIN work_sites ws ON ws.id = sh.site_id "
            "WHERE a.staff_id = $1 "
            "  AND a.status != 'rejected' "
            "  AND a.organization_id = $2 "
            "  AND sh.start_time < $3 "
            "  AND sh.end_time > $4",
            payload.staff_id, org_id, shift_end, shift_start,
        )
        if overlap_rows:
            conflict = overlap_rows[0]
            start = conflict["start_time"]
            end = conflict["end_time"]
            return JSONResponse(
                {"error": f"This staff member already has an overlapping assignment ({start} — {end})"},
                status_code=409,
            )

        score = payload.score if payload.score is not None else 0.0
        try:
            row = await conn.fetchrow(
                "INSERT INTO assignments (shift_id, staff_id, score, status, organization_id) "
                "VALUES ($1, $2, $3::float8, 'pending', $4) "
                "RETURNING id, shift_id, staff_id, score::double precision as score, status",
                id, payload.staff_id, score, org_id,
            )
        except Exception:
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        assignment_id = row["id"]
        assignment = {
            "id": assignment_id,
            "shift_id": row["shift_id"],
            "staff_id": row["staff_id"],
            "score": row["score"],
            "status": row["status"],
        }
        await audit_log.log(
            conn, org_id, user_id, "assignment.create", "assignment",
            assignment_id, None, assignment, None,
        )
        return JSONResponse(assignment, status_code=201)


@router.post("/{id}/confirm/{assignment_id}")
async def confirm_assignment(
    request: Request,
    payload: ConfirmPayload,
    id: int = Path(...),
    assignment_id: int = Path(...),
):
    """POST /{id}/confirm/{assignment_id} — confirm a pending assignment."""
    user_id, _, _, org_id = require_auth(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Look up assignment details to check for overlaps before confirming
        assignment_rows = await conn.fetch(
            "SELECT a.staff_id, sh.start_time, sh.end_time, sh.site_id, "
            "s.name as staff_name, s.email as staff_email, "
            "ws.name as site_name, o.name as org_name "
            "FROM assignments a "
            "JOIN shifts sh ON sh.id = a.shift_id "
            "JOIN staff s ON s.id = a.staff_id "
            "JOIN work_sites ws ON ws.id = sh.site_id "
            "JOIN organizations o ON o.id = a.organization_id "
            "WHERE a.id = $1 AND a.organization_id = $2 AND sh.deleted_at IS NULL",
            assignment_id, org_id,
        )
        if not assignment_rows:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)

        a = assignment_rows[0]
        staff_id = a["staff_id"]
        shift_start = a["start_time"]
        shift_end = a["end_time"]
        staff_name = a["staff_name"]
        staff_email = a["staff_email"]
        site_name = a["site_name"]
        org_name = a["org_name"]

        # Check for overlapping confirmed assignments — can't double-book staff
        overlap_rows = await conn.fetch(
            "SELECT a.id, sh.start_time::text, sh.end_time::text, ws.name AS site_name "
            "FROM assignments a "
            "JOIN shifts sh ON sh.id = a.shift_id "
            "JOIN work_sites ws ON ws.id = sh.site_id "
            "WHERE a.staff_id = $1 "
            "  AND a.id != $2 "
            "  AND a.status = 'confirmed' "
            "  AND a.organization_id = $3 "
            "  AND sh.start_time < $4 "
            "  AND sh.end_time > $5",
            staff_id, assignment_id, org_id, shift_end, shift_start,
        )
        if overlap_rows:
            conflict = overlap_rows[0]
            start = conflict["start_time"]
            end = conflict["end_time"]
            return JSONResponse(
                {"error": f"Cannot confirm: this staff member has a confirmed overlapping assignment ({start} — {end})"},
                status_code=409,
            )

        confirmed_by = payload.confirmed_by if payload.confirmed_by is not None else "admin"

        row = await conn.fetchrow(
            "UPDATE assignments SET status = 'confirmed', confirmed_by = $1, confirmed_at = NOW() "
            "WHERE id = $2 "
            "RETURNING id, shift_id, staff_id, score::double precision as score, status",
            confirmed_by, assignment_id,
        )
        if row is None:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)

        shift_id = row["shift_id"]
        confirmed_staff_id = row["staff_id"]

        # Update shift's assigned_staff_id so the board reflects the assignment
        await conn.execute(
            "UPDATE shifts SET assigned_staff_id = $1 WHERE id = $2",
            confirmed_staff_id, shift_id,
        )

        # Auto-fill check: if this shift is linked to a staffing_request and
        # confirmed assignments now meet min_staff, mark the request 'filled'.
        await conn.execute(
            "SELECT check_request_filled(s) FROM shifts s WHERE s.id = $1",
            shift_id,
        )

        # Send shift confirmation email to the staff member
        shift_date = shift_start.strftime("%A, %B %d, %Y")
        start_fmt = shift_start.strftime("%H:%M")
        end_fmt = shift_end.strftime("%H:%M")
        email_result = await email_service.send_shift_confirmation_email(
            to=staff_email,
            staff_name=staff_name,
            org_name=org_name,
            site_name=site_name,
            shift_date=shift_date,
            start_time=start_fmt,
            end_time=end_fmt,
        )
        if not email_result.get("sent"):
            print(f"[email] Shift confirmation email not sent (reason: {email_result.get('reason')})")

        assignment = {
            "id": row["id"],
            "shift_id": shift_id,
            "staff_id": confirmed_staff_id,
            "score": row["score"],
            "status": row["status"],
        }
        before = {"status": "pending", "id": assignment_id}
        await audit_log.log(
            conn, org_id, user_id, "assignment.confirm", "assignment",
            assignment_id, before, assignment, None,
        )
        return assignment


@router.get("/{id}/assignments")
async def shift_assignments(request: Request, id: int = Path(...)):
    """GET /{id}/assignments — get all assignments for a shift."""
    _, _, _, org_id = require_auth(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT a.id, a.shift_id, a.staff_id, a.score::double precision as score, "
            "a.status, a.organization_id, s.name as staff_name, s.email as staff_email "
            "FROM assignments a "
            "JOIN staff s ON s.id = a.staff_id "
            "JOIN shifts sh ON sh.id = a.shift_id "
            "WHERE a.shift_id = $1 AND a.organization_id = $2 AND sh.deleted_at IS NULL "
            "ORDER BY a.score DESC",
            id, org_id,
        )
        assignments = [
            {
                "id": r["id"],
                "shift_id": r["shift_id"],
                "staff_id": r["staff_id"],
                "score": r["score"],
                "status": r["status"],
                "staff_name": r["staff_name"],
                "staff_email": r["staff_email"],
            }
            for r in rows
        ]
        return {"assignments": assignments}