"""Sales inquiries route — contact form + admin CRUD endpoints.

Direct port of src/routes/sales_inquiries.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Routes:
  POST / (public, mounted at /api)                  — public contact form submission
  GET  / (admin, mounted at /api/admin/sales-inquiries)         — list + filter (super-user)
  PATCH /{id} (admin, mounted at /api/admin/sales-inquiries)    — update stage/agent (super-user)
  GET  /export (admin, mounted at /api/admin/sales-inquiries)   — CSV export (super-user)
"""
import csv
import io

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.services.email import send_sales_inquiry_notification

router = APIRouter()
admin_router = APIRouter(prefix="/sales-inquiries")


# ── Payloads ─────────────────────────────────────────────────────────────────


class SalesInquiryPayload(BaseModel):
    name: str
    email: str
    phone: str | None = None
    company_name: str | None = None
    staff_count: int | None = None
    industry: str | None = None
    message: str | None = None
    page_url: str | None = None


class UpdateInquiryPayload(BaseModel):
    pipeline_stage: str
    agent_id: int | None = None


# ── Public: Contact form ──────────────────────────────────────────────────────


def _escape_sql(s: str) -> str:
    """Escape single quotes for inline SQL (mirrors Rust escape_sql)."""
    return s.replace("'", "''")


def _source_from_url(page_url: str | None) -> str:
    if page_url and "/blog/" in page_url:
        return "blog"
    if page_url and "/industries/" in page_url:
        return "industry_page"
    return "landing_page"


@router.post("/")
async def create_inquiry(payload: SalesInquiryPayload, request: Request):
    """POST / — public contact form submission."""
    name = payload.name.strip()
    email = payload.email.strip().lower()
    if not name or not email:
        return JSONResponse(
            {"error": "Name and email are required."}, status_code=400
        )
    if "@" not in email or "." not in email:
        return JSONResponse(
            {"error": "Invalid email address."}, status_code=400
        )

    phone = (payload.phone or "").strip() or ""
    company_name = (payload.company_name or "").strip() or ""
    industry = payload.industry or ""
    message = (payload.message or "").strip() or ""
    territory = "Philadelphia metro"
    source = _source_from_url(payload.page_url)
    page_url = payload.page_url or ""

    staff_count_sql = str(payload.staff_count) if payload.staff_count is not None else "NULL"

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Inline INSERT (mirrors the Rust db::sales_inquiries::insert_inquiry which
        # builds the SQL string with escape_sql). All values are quote-escaped.
        sql = (
            "INSERT INTO sales_inquiries "
            "(name, email, phone, company_name, staff_count, industry, message, territory, source, page_url) "
            "VALUES ("
            f"'{_escape_sql(name)}', "
            f"'{_escape_sql(email)}', "
            f"'{_escape_sql(phone)}', "
            f"'{_escape_sql(company_name)}', "
            f"{staff_count_sql}, "
            f"'{_escape_sql(industry)}', "
            f"'{_escape_sql(message)}', "
            f"'{_escape_sql(territory)}', "
            f"'{_escape_sql(source)}', "
            f"'{_escape_sql(page_url)}'"
            ")"
        )
        try:
            await conn.execute(sql)
        except Exception as e:
            print(f"[sales_inquiries] Failed to insert sales inquiry: {e}")
            return JSONResponse(
                {"error": "Failed to record inquiry."}, status_code=500
            )

        # Fetch the id by email (most recently created)
        row = await conn.fetchrow(
            "SELECT id FROM sales_inquiries WHERE email = $1 ORDER BY created_at DESC LIMIT 1",
            email,
        )
        inquiry_id = row["id"] if row else None

    # Send notification email to Chris (best-effort, mirrors Rust)
    phone_for_email = (payload.phone or "").strip() or None
    company_for_email = (payload.company_name or "").strip() or None
    industry_for_email = payload.industry or None
    message_for_email = (payload.message or "").strip() or None
    try:
        await send_sales_inquiry_notification(
            name, email,
            phone_for_email, company_for_email,
            payload.staff_count, industry_for_email, message_for_email,
        )
    except Exception as e:
        print(f"[sales_inquiries] Failed to send sales inquiry notification: {e}")

    return JSONResponse({"ok": True, "id": inquiry_id}, status_code=201)


# ── Admin: List inquiries ──────────────────────────────────────────────────────


def _build_inquiry_where(stage: str | None, agent_id: int | None) -> tuple[str, list]:
    """Build the WHERE clause for inquiry list/count. Mirrors the Rust pattern."""
    clauses = ["1=1"]
    params: list = []
    if stage is not None:
        clauses.append(f"pipeline_stage = '{_escape_sql(stage)}'")
    if agent_id is not None:
        clauses.append(f"assigned_agent_id = {agent_id}")
    return " AND ".join(clauses), params


@admin_router.get("/")
async def list_inquiries(
    request: Request,
    stage: str | None = None,
    agent_id: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    """GET / — list + filter sales inquiries (super-user)."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if role != "super":
        return JSONResponse(
            {"error": "Super user access required."}, status_code=403
        )

    lim = min(limit if limit is not None else 50, 200)
    off = offset or 0

    where_clause, _ = _build_inquiry_where(stage, agent_id)
    sql = (
        "SELECT id, name, email, phone, company_name, staff_count, industry, "
        "message, territory, source, page_url, status, pipeline_stage, "
        "assigned_agent_id, created_at, updated_at "
        f"FROM sales_inquiries WHERE {where_clause} "
        f"ORDER BY created_at DESC LIMIT {lim} OFFSET {off}"
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
        inquiries = [
            {
                "id": r["id"],
                "name": r["name"],
                "email": r["email"],
                "phone": r["phone"],
                "company_name": r["company_name"],
                "staff_count": r["staff_count"],
                "industry": r["industry"],
                "message": r["message"],
                "territory": r["territory"],
                "source": r["source"],
                "page_url": r["page_url"],
                "status": r["status"],
                "pipeline_stage": r["pipeline_stage"],
                "assigned_agent_id": r["assigned_agent_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    return JSONResponse({"inquiries": inquiries})


# ── Admin: Update inquiry ──────────────────────────────────────────────────────


@admin_router.patch("/{id}")
async def update_inquiry(id: int, payload: UpdateInquiryPayload, request: Request):
    """PATCH /{id} — update stage/agent (super-user)."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if role != "super":
        return JSONResponse(
            {"error": "Super user access required."}, status_code=403
        )

    agent_part = f", assigned_agent_id = {payload.agent_id}" if payload.agent_id is not None else ""
    sql = (
        "UPDATE sales_inquiries SET "
        f"pipeline_stage = '{_escape_sql(payload.pipeline_stage)}', "
        f"updated_at = NOW(){agent_part} "
        f"WHERE id = {id}"
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(sql)
        except Exception as e:
            print(f"[sales_inquiries] Failed to update sales inquiry {id}: {e}")
            return JSONResponse(
                {"error": "Failed to update inquiry."}, status_code=500
            )

    return JSONResponse({"ok": True})


# ── Admin: CSV export ──────────────────────────────────────────────────────────


@admin_router.get("/export")
async def export_inquiries(request: Request):
    """GET /export — CSV export of all sales inquiries (super-user)."""
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
            "SELECT id, name, email, phone, company_name, staff_count, industry, "
            "message, territory, source, page_url, status, pipeline_stage, "
            "assigned_agent_id, created_at, updated_at "
            "FROM sales_inquiries ORDER BY created_at DESC"
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "name", "email", "phone", "company_name", "staff_count",
         "industry", "message", "territory", "source", "page_url", "status",
         "pipeline_stage", "assigned_agent_id", "created_at", "updated_at"]
    )
    for r in rows:
        writer.writerow(
            [r["id"], r["name"], r["email"], r["phone"], r["company_name"],
             r["staff_count"], r["industry"], r["message"], r["territory"],
             r["source"], r["page_url"], r["status"], r["pipeline_stage"],
             r["assigned_agent_id"], r["created_at"], r["updated_at"]]
        )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="sales-inquiries.csv"'},
    )