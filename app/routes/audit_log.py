"""Audit log route — audit log query and export endpoints (super-only).

Direct port of src/routes/audit_log.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

HIPAA §164.312(b): audit controls — exportable audit trail for compliance reviews.

Route (mounted at /api/admin):
  GET /audit-log — query and export audit log entries (super-only)
"""
import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.middleware.auth import require_super_session
from app.db.pool import get_pool

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_date(s: str, end_of_day: bool = False) -> datetime | None:
    """Parse a date string into a UTC datetime.

    Accepts either 'YYYY-MM-DDTHH:MM:SS' or 'YYYY-MM-DD'.
    For 'YYYY-MM-DD' with end_of_day=True, sets time to 23:59:59.
    """
    # Try full datetime first
    try:
        ndt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return ndt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Try date only
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59)
        return d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def csv_escape(s: str) -> str:
    """Escape a string for CSV: wrap in quotes if it contains commas, quotes, or
    newlines, and double any internal quotes.
    """
    if "," in s or '"' in s or "\n" in s:
        return s.replace('"', '""')
    return s


# ── GET /audit-log ────────────────────────────────────────────────────────────


@router.get("/audit-log")
async def get_audit_log(
    request: Request,
    organization_id: int | None = None,
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    action: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    format: str | None = None,
):
    """GET /audit-log — query and export audit log entries.

    Super-only. Supports JSON (default) and CSV export.
    """
    try:
        _uid, _email = require_super_session(request)
    except Exception:
        return JSONResponse(
            {"error": "Super-user access required"}, status_code=401
        )

    # Parse date strings into UTC datetimes
    from_dt = _parse_date(from_date, end_of_day=False) if from_date else None
    to_dt = _parse_date(to_date, end_of_day=True) if to_date else None

    # Build the dynamic query — mirrors the Rust db::audit_log::query pattern.
    query_str = (
        "SELECT id, organization_id, user_id, action, entity_type, entity_id, "
        "before, after, ip_address, created_at "
        "FROM audit_log WHERE 1=1"
    )
    params: list = []
    param_idx = 1

    if organization_id is not None:
        query_str += f" AND organization_id = ${param_idx}"
        params.append(organization_id)
        param_idx += 1
    if user_id is not None:
        query_str += f" AND user_id = ${param_idx}"
        params.append(user_id)
        param_idx += 1
    if entity_type is not None:
        query_str += f" AND entity_type = ${param_idx}"
        params.append(entity_type)
        param_idx += 1
    if entity_id is not None:
        query_str += f" AND entity_id = ${param_idx}"
        params.append(entity_id)
        param_idx += 1
    if action is not None:
        query_str += f" AND action = ${param_idx}"
        params.append(action)
        param_idx += 1
    if from_dt is not None:
        query_str += f" AND created_at >= ${param_idx}"
        params.append(from_dt)
        param_idx += 1
    if to_dt is not None:
        query_str += f" AND created_at <= ${param_idx}"
        params.append(to_dt)
        param_idx += 1

    lim = min(limit if limit is not None else 1000, 10000)
    off = offset or 0
    query_str += f" ORDER BY created_at DESC LIMIT {lim} OFFSET {off}"

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(query_str, *params)
        except Exception as e:
            print(f"[audit_log] Audit log query error: {e}")
            return JSONResponse(
                {"error": "Database error"}, status_code=500
            )

    fmt = format or "json"
    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ["id", "organization_id", "user_id", "action", "entity_type",
             "entity_id", "created_at", "ip_address", "before", "after"]
        )
        for r in rows:
            before = str(r["before"]) if r["before"] is not None else ""
            after = str(r["after"]) if r["after"] is not None else ""
            created = r["created_at"].isoformat() if r["created_at"] else ""
            org_id_str = str(r["organization_id"]) if r["organization_id"] is not None else ""
            uid_str = str(r["user_id"]) if r["user_id"] is not None else ""
            eid_str = str(r["entity_id"]) if r["entity_id"] is not None else ""
            ip = r["ip_address"] or ""
            writer.writerow(
                [r["id"], org_id_str, uid_str, csv_escape(r["action"]),
                 csv_escape(r["entity_type"]), eid_str, created, ip,
                 csv_escape(before), csv_escape(after)]
            )

        return Response(
            content=output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="audit-log.csv"',
            },
        )

    # Default: JSON
    entries = [
        {
            "id": r["id"],
            "organization_id": r["organization_id"],
            "user_id": r["user_id"],
            "action": r["action"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "before": r["before"],
            "after": r["after"],
            "ip_address": r["ip_address"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return JSONResponse({"entries": entries, "count": len(entries)})