"""Assignments route — list, confirm, reassign, unassign.

Direct port of src/routes/assignments.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version. org_id comes from the session.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db import audit_log
from app.db.shifts import find_matching_staff
from app.services.email import send_shift_confirmation_email, send_shift_unassigned_email

router = APIRouter()


class ConfirmPayload(BaseModel):
    confirmed_by: str | None = None


class ReassignPayload(BaseModel):
    new_staff_id: int
    score: float | None = None


# ── GET / — list all assignments (optionally filtered) ────────────────────────

@router.get("/")
async def list_assignments(request: Request):
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Query filters (all optional)
        staff_id = request.query_params.get("staff_id")
        status = request.query_params.get("status")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        # Build the query dynamically — mirrors the Rust pattern exactly.
        query_str = (
            "SELECT a.id, a.shift_id, a.staff_id, a.score::double precision as score, "
            "a.status, a.organization_id, a.confirmed_by, a.confirmed_at::text, "
            "s.name as staff_name, s.email as staff_email, "
            "sh.start_time::text, sh.end_time::text, sh.site_id, ws.name as site_name "
            "FROM assignments a "
            "JOIN staff s ON s.id = a.staff_id "
            "JOIN shifts sh ON sh.id = a.shift_id "
            "JOIN work_sites ws ON ws.id = sh.site_id "
            "WHERE a.organization_id = $1"
        )
        params: list = [org_id]
        param_idx = 2

        if staff_id is not None:
            query_str += f" AND a.staff_id = ${param_idx}"
            params.append(int(staff_id))
            param_idx += 1

        if status is not None:
            query_str += f" AND a.status = ${param_idx}"
            params.append(status)
            param_idx += 1

        if from_date is not None:
            try:
                from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(
                    hour=0, minute=0, second=0, tzinfo=timezone.utc
                )
            except ValueError:
                return JSONResponse(
                    {"error": "Invalid from_date format, expected YYYY-MM-DD"},
                    status_code=400,
                )
            query_str += f" AND sh.start_time >= ${param_idx}"
            params.append(from_dt)
            param_idx += 1

        if to_date is not None:
            try:
                to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
            except ValueError:
                return JSONResponse(
                    {"error": "Invalid to_date format, expected YYYY-MM-DD"},
                    status_code=400,
                )
            query_str += f" AND sh.start_time <= ${param_idx}"
            params.append(to_dt)
            param_idx += 1

        query_str += " ORDER BY sh.start_time"

        rows = await conn.fetch(query_str, *params)
        assignments = [
            {
                "id": r["id"],
                "shift_id": r["shift_id"],
                "staff_id": r["staff_id"],
                "score": r["score"],
                "status": r["status"],
                "staff_name": r["staff_name"],
                "staff_email": r["staff_email"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "site_id": r["site_id"],
                "site_name": r["site_name"],
                "confirmed_by": r["confirmed_by"],
                "confirmed_at": r["confirmed_at"],
            }
            for r in rows
        ]

    return JSONResponse({"assignments": assignments})


# ── POST /{id}/confirm — confirm a pending assignment ─────────────────────────

@router.post("/{id}/confirm")
async def confirm_assignment(id: int, payload: ConfirmPayload, request: Request):
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Look up assignment details to check for overlaps before confirming.
        assignment_rows = await conn.fetch(
            """SELECT a.staff_id, sh.start_time, sh.end_time,
                      s.name as staff_name, s.email as staff_email,
                      ws.name as site_name,
                      o.name as org_name
               FROM assignments a
               JOIN shifts sh ON sh.id = a.shift_id
               JOIN staff s ON s.id = a.staff_id
               JOIN work_sites ws ON ws.id = sh.site_id
               JOIN organizations o ON o.id = a.organization_id
               WHERE a.id = $1 AND a.organization_id = $2""",
            id, org_id,
        )
        if not assignment_rows:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)

        row0 = assignment_rows[0]
        staff_id = row0["staff_id"]
        shift_start = row0["start_time"]
        shift_end = row0["end_time"]
        staff_name = row0["staff_name"]
        staff_email = row0["staff_email"]
        site_name = row0["site_name"]
        org_name = row0["org_name"]

        # Check for overlapping confirmed assignments — can't double-book staff.
        overlap_rows = await conn.fetch(
            """SELECT a.id, sh.start_time::text, sh.end_time::text, ws.name AS site_name
               FROM assignments a
               JOIN shifts sh ON sh.id = a.shift_id
               JOIN work_sites ws ON ws.id = sh.site_id
               WHERE a.staff_id = $1
                 AND a.id != $2
                 AND a.status = 'confirmed'
                 AND a.organization_id = $3
                 AND sh.start_time < $4
                 AND sh.end_time > $5""",
            staff_id, id, org_id, shift_end, shift_start,
        )
        if overlap_rows:
            conflict = overlap_rows[0]
            start = conflict["start_time"]
            end = conflict["end_time"]
            return JSONResponse(
                {"error": f"Cannot confirm: this staff member has a confirmed overlapping assignment ({start} — {end})"},
                status_code=409,
            )

        confirmed_by = payload.confirmed_by or "admin"

        updated = await conn.fetchrow(
            """UPDATE assignments SET status = 'confirmed', confirmed_by = $1, confirmed_at = NOW()
               WHERE id = $2
               RETURNING id, shift_id, staff_id, score::double precision as score, status""",
            confirmed_by, id,
        )
        if updated is None:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)

        shift_id = updated["shift_id"]
        confirmed_staff_id = updated["staff_id"]

        # Update shift's assigned_staff_id so the board reflects the assignment.
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

        # Send shift confirmation email to the staff member.
        shift_date = shift_start.strftime("%A, %B %d, %Y")
        start_fmt = shift_start.strftime("%H:%M")
        end_fmt = shift_end.strftime("%H:%M")
        email_result = await send_shift_confirmation_email(
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
            "id": updated["id"],
            "shift_id": shift_id,
            "staff_id": confirmed_staff_id,
            "score": updated["score"],
            "status": updated["status"],
        }
        before = {"id": id, "status": "pending"}
        await audit_log.log(
            conn, org_id, user_id,
            "assignment.confirm", "assignment", id,
            before, assignment, None,
        )

    return JSONResponse(assignment)


# ── PATCH /{id}/reassign — change the staff member on an assignment ───────────

@router.patch("/{id}/reassign")
async def reassign_assignment(id: int, payload: ReassignPayload, request: Request):
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Look up the existing assignment and verify org ownership.
        assignment_rows = await conn.fetch(
            """SELECT a.staff_id, a.shift_id, a.status
               FROM assignments a
               JOIN shifts sh ON sh.id = a.shift_id
               JOIN work_sites ws ON ws.id = sh.site_id
               WHERE a.id = $1 AND ws.organization_id = $2""",
            id, org_id,
        )
        if not assignment_rows:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)

        old_staff_id = assignment_rows[0]["staff_id"]
        shift_id = assignment_rows[0]["shift_id"]

        # Verify new staff member exists and belongs to org.
        staff_rows = await conn.fetch(
            "SELECT id FROM staff WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL",
            payload.new_staff_id, org_id,
        )
        if not staff_rows:
            return JSONResponse(
                {"error": "Staff member not found or belongs to another organization"},
                status_code=400,
            )

        # Get the shift's time range for overlap checking.
        shift_rows = await conn.fetch(
            "SELECT start_time, end_time FROM shifts WHERE id = $1",
            shift_id,
        )
        shift_start = shift_rows[0]["start_time"]
        shift_end = shift_rows[0]["end_time"]

        # Check for overlapping assignments for the NEW staff member (excluding current).
        overlap_rows = await conn.fetch(
            """SELECT a.id, sh.start_time::text, sh.end_time::text, ws.name AS site_name
               FROM assignments a
               JOIN shifts sh ON sh.id = a.shift_id
               JOIN work_sites ws ON ws.id = sh.site_id
               WHERE a.staff_id = $1
                 AND a.id != $2
                 AND a.status != 'rejected'
                 AND ws.organization_id = $3
                 AND sh.start_time < $4
                 AND sh.end_time > $5""",
            payload.new_staff_id, id, org_id, shift_end, shift_start,
        )
        if overlap_rows:
            conflict = overlap_rows[0]
            start = conflict["start_time"]
            end = conflict["end_time"]
            return JSONResponse(
                {"error": f"Cannot reassign: the new staff member has an overlapping assignment ({start} — {end})"},
                status_code=409,
            )

        # Compute match score for the new staff member against this shift.
        # Uses the same weights as the match route (proximity=0.3, skills=0.4, availability=0.3).
        score = payload.score if payload.score is not None else 0.0
        candidates = await find_matching_staff(conn, shift_id, org_id, 0.3, 0.4, 0.3)
        if candidates is not None:
            for c in candidates:
                if c["staff_id"] == payload.new_staff_id:
                    score = c["score"]
                    break
        # else: keep payload.score (or 0.0)

        updated = await conn.fetchrow(
            """UPDATE assignments SET staff_id = $1, score = $2::float8, updated_at = NOW()
               WHERE id = $3
               RETURNING id, shift_id, staff_id, score::double precision as score, status""",
            payload.new_staff_id, score, id,
        )
        if updated is None:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)

        # If this is a confirmed assignment, update the shift's assigned_staff_id too.
        status = updated["status"]
        if status == "confirmed":
            await conn.execute(
                "UPDATE shifts SET assigned_staff_id = $1 WHERE id = $2",
                payload.new_staff_id, updated["shift_id"],
            )

        assignment = {
            "id": updated["id"],
            "shift_id": updated["shift_id"],
            "staff_id": updated["staff_id"],
            "score": updated["score"],
            "status": updated["status"],
        }
        before = {"id": id, "staff_id": old_staff_id}
        await audit_log.log(
            conn, org_id, user_id,
            "assignment.reassign", "assignment", id,
            before, assignment, None,
        )

    return JSONResponse(assignment)


# ── DELETE /{id} — unassign (delete) an assignment ────────────────────────────

@router.delete("/{id}")
async def unassign_assignment(id: int, request: Request):
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Look up the assignment to verify org ownership and get shift/staff info.
        assignment_rows = await conn.fetch(
            """SELECT a.shift_id, a.status, a.staff_id,
                      s.name as staff_name, s.email as staff_email,
                      sh.start_time, sh.end_time,
                      ws.name as site_name,
                      o.name as org_name
               FROM assignments a
               JOIN shifts sh ON sh.id = a.shift_id
               JOIN staff s ON s.id = a.staff_id
               JOIN work_sites ws ON ws.id = sh.site_id
               JOIN organizations o ON o.id = a.organization_id
               WHERE a.id = $1 AND ws.organization_id = $2""",
            id, org_id,
        )
        if not assignment_rows:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)

        row0 = assignment_rows[0]
        shift_id = row0["shift_id"]
        status = row0["status"]
        staff_id = row0["staff_id"]
        staff_name = row0["staff_name"]
        staff_email = row0["staff_email"]
        site_name = row0["site_name"]
        org_name = row0["org_name"]
        shift_start = row0["start_time"]
        shift_end = row0["end_time"]

        # Delete the assignment.
        await conn.execute("DELETE FROM assignments WHERE id = $1", id)

        # If this was a confirmed assignment, clear the shift's assigned_staff_id
        # (only if it points to this staff member — another confirmed assignment
        # may have already updated it).
        if status == "confirmed":
            await conn.execute(
                "UPDATE shifts SET assigned_staff_id = NULL WHERE id = $1 AND assigned_staff_id = $2",
                shift_id, staff_id,
            )

            # Re-check if the shift still has any confirmed assignments.
            # If another assignment is confirmed, set assigned_staff_id to that staff.
            other_confirmed = await conn.fetchrow(
                """SELECT staff_id FROM assignments
                   WHERE shift_id = $1 AND status = 'confirmed'
                   ORDER BY confirmed_at LIMIT 1""",
                shift_id,
            )
            if other_confirmed is not None:
                new_staff_id = other_confirmed["staff_id"]
                await conn.execute(
                    "UPDATE shifts SET assigned_staff_id = $1 WHERE id = $2",
                    new_staff_id, shift_id,
                )

        # Re-check request filled status (the shift may now be underfilled).
        await conn.execute(
            "SELECT check_request_filled(s) FROM shifts s WHERE s.id = $1",
            shift_id,
        )

        # Send shift unassigned email to the staff member.
        shift_date = shift_start.strftime("%A, %B %d, %Y")
        start_fmt = shift_start.strftime("%H:%M")
        end_fmt = shift_end.strftime("%H:%M")
        email_result = await send_shift_unassigned_email(
            to=staff_email,
            staff_name=staff_name,
            org_name=org_name,
            site_name=site_name,
            shift_date=shift_date,
            start_time=start_fmt,
            end_time=end_fmt,
        )
        if not email_result.get("sent"):
            print(f"[email] Shift unassigned email not sent (reason: {email_result.get('reason')})")

        before = {"id": id, "shift_id": shift_id, "staff_id": staff_id, "status": status}
        await audit_log.log(
            conn, org_id, user_id,
            "assignment.delete", "assignment", id,
            before, None, None,
        )

    return JSONResponse({"ok": True, "message": "Assignment removed"})