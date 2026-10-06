"""Admin-only MCP tools — read-only SQL."""
import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _error_result, _json_result


async def run_sql(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    # run_sql executes caller-supplied SQL directly against the shared,
    # multi-tenant connection pool, so — unlike every other tool — it cannot
    # be constrained to a single organization_id. It must therefore be limited
    # to the platform operator ("super"), NOT the per-org "admin" role.
    #
    # Every self-serve API key is mapped to "admin" (see server.py), so gating
    # on "admin" let any trial/paying organization run e.g.
    # `SELECT email, password_hash FROM users` and read every other tenant's
    # data — a cross-tenant confidentiality break (CWE-862). The "super" role
    # is only ever issued through a signed OAuth JWT (never an API key), so it
    # stays with the platform operator.
    if role != "super":
        return _error_result("run_sql is restricted to platform operators (the 'super' role).")
    sql = arguments["sql"].strip()
    normalized = sql.lower()
    if not normalized.startswith("select"):
        return _error_result("Only SELECT queries are allowed")
    forbidden = ["insert into", "update ", "delete from", "drop ", "alter ", "create "]
    if any(w in normalized for w in forbidden):
        return _error_result("Only SELECT queries are allowed")
    # Defence in depth: run inside a READ ONLY transaction so the database
    # itself rejects any write, independent of the string checks above.
    async with conn.transaction(readonly=True):
        rows = await conn.fetch(sql)
    return _json_result([dict(r) for r in rows])