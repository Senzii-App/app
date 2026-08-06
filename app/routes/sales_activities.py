"""Sales activities route — activity log endpoints for the sales pipeline.

Direct port of src/routes/sales_activities.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Admin routes (mounted at /api/admin):
  POST /{inquiry_id}/activities  — add note/call/email (super-user)
  GET  /{inquiry_id}/activities   — list activities for an inquiry (super-user)
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool

admin_router = APIRouter()


# ── Payloads ─────────────────────────────────────────────────────────────────


class NewActivityPayload(BaseModel):
    activity_type: str
    summary: str


# ── Handlers ──────────────────────────────────────────────────────────────────


@admin_router.post("/{inquiry_id}/activities")
async def create_activity(inquiry_id: int, payload: NewActivityPayload, request: Request):
    """POST /{inquiry_id}/activities — add a note/call/email (super-user)."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if role != "super":
        return JSONResponse(
            {"error": "Super user access required."}, status_code=403
        )

    if not payload.summary.strip():
        return JSONResponse(
            {"error": "Activity summary is required."}, status_code=400
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO sales_activities (inquiry_id, agent_id, activity_type, summary)
                   VALUES ($1, $2, $3, $4)
                   RETURNING id""",
                inquiry_id, None, payload.activity_type, payload.summary.strip(),
            )
        except Exception as e:
            print(f"[sales_activities] Failed to create sales activity: {e}")
            return JSONResponse(
                {"error": "Failed to create activity."}, status_code=500
            )

    return JSONResponse({"ok": True, "id": row["id"]}, status_code=201)


@admin_router.get("/{inquiry_id}/activities")
async def list_activities(inquiry_id: int, request: Request):
    """GET /{inquiry_id}/activities — list activities for an inquiry (super-user)."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if role != "super":
        return JSONResponse(
            {"error": "Super user access required."}, status_code=403
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, inquiry_id, agent_id, activity_type, summary, created_at
               FROM sales_activities WHERE inquiry_id = $1
               ORDER BY created_at DESC""",
            inquiry_id,
        )
        activities = [
            {
                "id": r["id"],
                "inquiry_id": r["inquiry_id"],
                "agent_id": r["agent_id"],
                "activity_type": r["activity_type"],
                "summary": r["summary"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    return JSONResponse({"activities": activities})