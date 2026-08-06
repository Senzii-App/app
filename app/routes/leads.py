"""Leads route — lead capture API endpoint.

Direct port of src/routes/leads.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Routes:
  POST / (public, mounted at /api)  — accepts email + tool data from lead magnet tools
  GET  / (admin, mounted at /api/admin/leads)      — returns all leads (super-only)
  GET  /export (admin, mounted at /api/admin/leads) — CSV export (super-only)
"""
import csv
import io

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.db.pool import get_pool

router = APIRouter()
admin_router = APIRouter(prefix="/leads")


# ── POST /api/leads ───────────────────────────────────────────────────────────


class LeadPayload(BaseModel):
    email: str
    tool: str
    industry: str | None = None
    staff_size: int | None = None
    calculated_cost: float | None = None
    pain_point: str | None = None
    quiz_scores: dict | None = None
    extra_data: dict | None = None


# Valid lead magnet tool identifiers (must match the Rust list exactly).
VALID_TOOLS = {
    "cost-calculator", "pain-point-quiz", "template-generator",
    "coverage-analyzer", "compliance-checker", "staffing-calculator",
    "overtime-calculator", "shift-conflict-detector", "labor-law-lookup",
    "workload-balancer", "on-call-rotation", "shift-swap-manager",
    "scheduling-maturity-assessment", "schedule-visualizer",
    "shift-cost-estimator", "team-capacity-planner", "scheduling-scorecard",
    "labor-budget-planner", "shift-pattern-optimizer",
    "turnover-risk-predictor", "no-show-impact-calculator",
    "labor-cost-benchmarker", "schedule-scenario-planner",
    "break-coverage-planner", "skill-coverage-matrix",
    "shift-handover-generator", "schedule-fairness-auditor",
    "weekly-schedule-generator", "shift-bidding-optimizer",
    "scheduling-roi-estimator",
}


@router.post("/")
async def create_lead(payload: LeadPayload, request: Request):
    """Public endpoint — accepts lead data from any of the lead magnet tools.

    Returns 201 Created on success, 400 for bad input, 500 for server errors.
    Idempotent on email+tool — re-submitting the same email+tool updates the data
    rather than creating a duplicate.
    """
    # Basic email validation
    email = payload.email.strip().lower()
    if not email or "@" not in email or "." not in email or len(email) < 5:
        return JSONResponse({"error": "Invalid email address"}, status_code=400)

    # Validate tool name
    if payload.tool not in VALID_TOOLS:
        return JSONResponse({"error": "Invalid tool identifier"}, status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check if a lead with this email+tool already exists
        existing = await conn.fetchrow(
            "SELECT id FROM leads WHERE LOWER(email) = LOWER($1) AND tool = $2 LIMIT 1",
            email, payload.tool,
        )

        if existing is not None:
            lead_id = existing["id"]
            # Update existing lead with new data
            await conn.execute(
                """UPDATE leads SET industry = $3, staff_size = $4, calculated_cost = $5,
                   pain_point = $6, quiz_scores = $7, extra_data = $8, updated_at = NOW()
                   WHERE id = $1 AND (email = $2 OR email = $2)""",
                lead_id, email, payload.industry, payload.staff_size,
                payload.calculated_cost, payload.pain_point,
                payload.quiz_scores, payload.extra_data,
            )
        else:
            row = await conn.fetchrow(
                """INSERT INTO leads
                   (email, tool, industry, staff_size, calculated_cost, pain_point, quiz_scores, extra_data)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   RETURNING id""",
                email, payload.tool, payload.industry, payload.staff_size,
                payload.calculated_cost, payload.pain_point,
                payload.quiz_scores, payload.extra_data,
            )
            lead_id = row["id"]

    return JSONResponse(
        {"ok": True, "id": lead_id, "message": "Lead recorded successfully"},
        status_code=201,
    )


# ── GET /api/admin/leads ──────────────────────────────────────────────────────


@admin_router.get("/")
async def get_leads(request: Request, limit: int | None = None, offset: int | None = None):
    """Returns all captured leads. Requires super role."""
    role = request.session.get("role")
    if role != "super":
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    lim = limit if limit is not None else 100
    lim = max(1, min(lim, 1000))
    off = max(offset or 0, 0)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, email, tool, industry, staff_size, calculated_cost,
                      pain_point, quiz_scores, extra_data, created_at
               FROM leads ORDER BY created_at DESC LIMIT $1 OFFSET $2""",
            lim, off,
        )
        leads = [
            {
                "id": r["id"],
                "email": r["email"],
                "tool": r["tool"],
                "industry": r["industry"],
                "staff_size": r["staff_size"],
                "calculated_cost": r["calculated_cost"],
                "pain_point": r["pain_point"],
                "quiz_scores": r["quiz_scores"],
                "extra_data": r["extra_data"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    return JSONResponse(leads)


# ── GET /api/admin/leads/export ───────────────────────────────────────────────


@admin_router.get("/export")
async def export_leads(request: Request):
    """CSV export of all leads. Requires super role."""
    role = request.session.get("role")
    if role != "super":
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, email, tool, industry, staff_size, calculated_cost,
                      pain_point, quiz_scores, extra_data, created_at
               FROM leads ORDER BY created_at DESC"""
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "email", "tool", "industry", "staff_size", "calculated_cost",
         "pain_point", "quiz_scores", "extra_data", "created_at"]
    )
    for r in rows:
        writer.writerow(
            [r["id"], r["email"], r["tool"], r["industry"], r["staff_size"],
             r["calculated_cost"], r["pain_point"],
             r["quiz_scores"], r["extra_data"], r["created_at"]]
        )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )