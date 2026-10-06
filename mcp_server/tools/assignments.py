"""Assignment MCP tools — list, create, unassign shift assignments."""
import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _audit_log, _error_result, _json_result


async def list_assignments(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    if arguments.get("shift_id"):
        rows = await conn.fetch(
            """SELECT a.*, s.name as staff_name, s.email as staff_email
               FROM assignments a JOIN staff s ON s.id = a.staff_id
               WHERE a.organization_id = $1 AND a.shift_id = $2 ORDER BY a.score DESC""",
            org_id, arguments["shift_id"],
        )
    else:
        rows = await conn.fetch(
            """SELECT a.*, s.name as staff_name, s.email as staff_email
               FROM assignments a JOIN staff s ON s.id = a.staff_id
               WHERE a.organization_id = $1 ORDER BY a.score DESC""",
            org_id,
        )
    return _json_result([dict(r) for r in rows])


async def create_assignment(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    shift_id = arguments["shift_id"]
    staff_id = arguments["staff_id"]
    # The shift and staff member must both belong to the caller's org —
    # otherwise a caller could assign its staff to another org's shift or
    # attach another org's staff to its own shifts (cross-tenant write).
    owner = await conn.fetchval(
        "SELECT organization_id FROM shifts WHERE id = $1 AND deleted_at IS NULL",
        shift_id,
    )
    if owner != org_id:
        return _error_result("Shift not found")
    owner = await conn.fetchval(
        "SELECT organization_id FROM staff WHERE id = $1 AND deleted_at IS NULL",
        staff_id,
    )
    if owner != org_id:
        return _error_result("Staff member not found")
    # Check overlap
    overlap = await conn.fetch(
        """SELECT a.id FROM assignments a
           JOIN shifts sh ON sh.id = a.shift_id
           JOIN shifts new_shift ON new_shift.id = $1
           WHERE a.staff_id = $2 AND a.status != 'rejected'
           AND sh.start_time < new_shift.end_time AND sh.end_time > new_shift.start_time""",
        shift_id, staff_id,
    )
    if overlap:
        return _error_result("Staff member already has an overlapping assignment")
    row = await conn.fetchrow(
        "INSERT INTO assignments (shift_id, staff_id, organization_id) VALUES ($1, $2, $3) RETURNING *",
        shift_id, staff_id, org_id,
    )
    await _audit_log(conn, org_id, user_id, "assignment.create", "assignment", row["id"], None, dict(row))
    return _json_result(dict(row))


async def unassign_assignment(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    # The org filter is the security boundary: without it any tenant could
    # delete another organization's assignment by guessing its id.
    deleted = await conn.fetchval(
        "DELETE FROM assignments WHERE id = $1 AND organization_id = $2 RETURNING id",
        arguments["id"], org_id,
    )
    if deleted is None:
        return _error_result("Assignment not found")
    await _audit_log(conn, org_id, user_id, "assignment.delete", "assignment", arguments["id"], None, None)
    return _json_result({"ok": True, "message": "Assignment removed"})
