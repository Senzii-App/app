"""Organization-level certification MCP tools — list, create, update, delete."""
import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _audit_log, _error_result, _json_result


async def list_certifications(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    rows = await conn.fetch(
        "SELECT * FROM organization_certifications WHERE organization_id = $1 ORDER BY name", org_id)
    return _json_result([dict(r) for r in rows])


async def create_certification(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    name_val = arguments["name"].strip()
    if not name_val:
        return _error_result("name is required")
    # Idempotent — return existing if present
    existing = await conn.fetchrow(
        "SELECT * FROM organization_certifications WHERE organization_id = $1 AND LOWER(name) = LOWER($2)",
        org_id, name_val,
    )
    if existing:
        return _json_result(dict(existing))
    row = await conn.fetchrow(
        "INSERT INTO organization_certifications (organization_id, name) VALUES ($1, $2) RETURNING *",
        org_id, name_val,
    )
    await _audit_log(conn, org_id, user_id, "org_certification.create", "organization_certification", row["id"], None, dict(row))
    return _json_result(dict(row))


async def update_certification(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    name_val = arguments["name"].strip()
    if not name_val:
        return _error_result("name is required")
    row = await conn.fetchrow(
        "UPDATE organization_certifications SET name = $1 WHERE id = $2 AND organization_id = $3 RETURNING *",
        name_val, arguments["id"], org_id,
    )
    if row is None:
        return _error_result("Certification not found")
    await _audit_log(conn, org_id, user_id, "org_certification.update", "organization_certification", arguments["id"], None, dict(row))
    return _json_result(dict(row))


async def delete_certification(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    await conn.execute(
        "DELETE FROM organization_certifications WHERE id = $1 AND organization_id = $2",
        arguments["id"], org_id,
    )
    await _audit_log(conn, org_id, user_id, "org_certification.delete", "organization_certification", arguments["id"], None, None)
    return _json_result({"ok": True, "message": "Certification deleted"})