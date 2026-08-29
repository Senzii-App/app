"""Admin-only MCP tools — read-only SQL."""
import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _error_result, _json_result


async def run_sql(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    if role not in ("admin", "super"):
        return _error_result("run_sql is admin-only — your role does not have access to this tool.")
    sql = arguments["sql"].strip()
    normalized = sql.lower()
    if not normalized.startswith("select"):
        return _error_result("Only SELECT queries are allowed")
    forbidden = ["insert into", "update ", "delete from", "drop ", "alter ", "create "]
    if any(w in normalized for w in forbidden):
        return _error_result("Only SELECT queries are allowed")
    rows = await conn.fetch(sql)
    return _json_result([dict(r) for r in rows])