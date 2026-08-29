"""Shared helpers and types for MCP tool functions."""
import json
from typing import Any, Awaitable, Callable

import asyncpg
from mcp.types import CallToolResult, TextContent

# Every handler has the signature (conn, org_id, user_id, role, arguments)
# and returns a CallToolResult.
ToolHandler = Callable[[asyncpg.Connection, int, int | None, str, dict], Awaitable[CallToolResult]]


def _json_result(data: Any) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, indent=2, default=str))],
        isError=False,
    )


def _error_result(msg: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=msg)],
        isError=True,
    )


async def _audit_log(conn: asyncpg.Connection, org_id: int, user_id: int | None, action: str, entity_type: str, entity_id: int | None, before: dict | None, after: dict | None):
    try:
        await conn.execute(
            """INSERT INTO audit_log (organization_id, user_id, action, entity_type, entity_id, before, after)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            org_id, user_id, action, entity_type, entity_id,
            json.dumps(before) if before else None,
            json.dumps(after) if after else None,
        )
    except Exception as e:
        print(f"[audit_log] Failed: {e}")