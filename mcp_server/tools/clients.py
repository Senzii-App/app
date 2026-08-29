"""Client MCP tools — list, create clients; client magic links."""
import uuid

import asyncpg
from mcp.types import CallToolResult

from mcp_server.config import BASE_URL
from mcp_server.tools.common import _audit_log, _error_result, _json_result


async def list_clients(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    rows = await conn.fetch(
        "SELECT * FROM clients WHERE organization_id = $1 ORDER BY id", org_id)
    return _json_result([dict(r) for r in rows])


async def create_client(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    row = await conn.fetchrow(
        """INSERT INTO clients (name, email, phone, company_name, organization_id)
           VALUES ($1, $2, $3, $4, $5) RETURNING *""",
        arguments["name"], arguments["email"], arguments.get("phone"),
        arguments.get("company_name"), org_id,
    )
    await _audit_log(conn, org_id, user_id, "client.create", "client", row["id"], None, dict(row))
    return _json_result(dict(row))


async def send_client_magic_link(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    client_id = arguments["id"]
    # Verify client exists and belongs to this org (Rust contract)
    client_row = await conn.fetchrow(
        "SELECT id, email, name, company_name FROM clients WHERE id = $1 AND organization_id = $2",
        client_id, org_id,
    )
    if client_row is None:
        return _error_result("Client not found")
    token = str(uuid.uuid4())
    row = await conn.fetchrow(
        """INSERT INTO client_magic_tokens (client_id, client_email, client_name, company_name, organization_id, token, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '7 days')
           RETURNING token, expires_at""",
        client_id, client_row["email"], client_row["name"],
        client_row.get("company_name"), org_id, token,
    )
    link = f"{BASE_URL}/client/login?token={row['token']}"
    return _json_result({
        "token": row["token"],
        "expires_at": row["expires_at"],
        "client_email": client_row["email"],
        "client_name": client_row["name"],
        "link": link,
    })