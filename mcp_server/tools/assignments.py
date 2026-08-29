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
    await conn.execute("DELETE FROM assignments WHERE id = $1", arguments["id"])
    await _audit_log(conn, org_id, user_id, "assignment.delete", "assignment", arguments["id"], None, None)
    return _json_result({"ok": True, "message": "Assignment removed"})