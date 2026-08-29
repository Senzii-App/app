"""Shift MCP tools — list, delete shifts; candidate matching."""
import json

import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _audit_log, _error_result, _json_result


async def list_shifts(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    if arguments.get("site_id"):
        rows = await conn.fetch(
            """SELECT s.*, ws.name as site_name FROM shifts s
               JOIN work_sites ws ON ws.id = s.site_id
               WHERE s.organization_id = $1 AND s.deleted_at IS NULL AND s.site_id = $2
               ORDER BY s.start_time""",
            org_id, arguments["site_id"],
        )
    else:
        rows = await conn.fetch(
            """SELECT s.*, ws.name as site_name FROM shifts s
               JOIN work_sites ws ON ws.id = s.site_id
               WHERE s.organization_id = $1 AND s.deleted_at IS NULL
               ORDER BY s.start_time""",
            org_id,
        )
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["required_skills"] = json.loads(d["required_skills"]) if isinstance(d["required_skills"], str) else d["required_skills"]
        except Exception:
            d["required_skills"] = []
        out.append(d)
    return _json_result(out)


async def delete_shift(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    shift_id = arguments["id"]
    # Check for confirmed assignments
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM assignments WHERE shift_id = $1 AND organization_id = $2 AND status = 'confirmed'",
        shift_id, org_id,
    )
    if count > 0:
        return _error_result(f"Cannot delete shift — {count} confirmed assignment(s). Remove or reassign staff first.")
    await conn.execute(
        "UPDATE shifts SET deleted_at = NOW(), assigned_staff_id = NULL WHERE id = $1 AND organization_id = $2",
        shift_id, org_id,
    )
    await conn.execute(
        "UPDATE assignments SET status = 'rejected' WHERE shift_id = $1 AND organization_id = $2 AND status = 'pending'",
        shift_id, org_id,
    )
    await _audit_log(conn, org_id, user_id, "shift.delete", "shift", shift_id, None, None)
    return _json_result({"ok": True, "message": "Shift deleted"})


async def find_candidates(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    from app.db.shifts import find_matching_staff
    candidates = await find_matching_staff(conn, arguments["shift_id"], org_id, 0.3, 0.4, 0.3)
    if candidates is None:
        return _error_result("Shift not found")
    return _json_result(candidates)