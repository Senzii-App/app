"""Work site MCP tools — list, create work sites."""
import json

import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _audit_log, _json_result


async def list_work_sites(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    rows = await conn.fetch(
        "SELECT * FROM work_sites WHERE organization_id = $1 ORDER BY name", org_id)
    return _json_result([dict(r) for r in rows])


async def create_work_site(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    row = await conn.fetchrow(
        """INSERT INTO work_sites (name, address, latitude, longitude, required_skills, timezone, organization_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
        arguments["name"], arguments.get("address"),
        arguments.get("latitude"), arguments.get("longitude"),
        json.dumps(arguments.get("required_skills", [])),
        arguments.get("timezone", "UTC"), org_id,
    )
    await _audit_log(conn, org_id, user_id, "work_site.create", "work_site", row["id"], None, dict(row))
    return _json_result(dict(row))