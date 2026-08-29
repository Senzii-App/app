"""Org metrics MCP tool — dashboard metrics."""
import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _json_result


async def get_org_metrics(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    # Port of mcp_server/src/db/metrics.rs (Rust contract):
    # open_shifts, pending_requests, unassigned_staff, recent_confirmed
    row = await conn.fetchrow(
        """WITH
        today AS (
          SELECT CURRENT_DATE AS d
        ),
        open_shifts AS (
          SELECT COUNT(*) as c
          FROM shifts sh
          JOIN work_sites ws ON ws.id = sh.site_id
          WHERE ws.organization_id = $1
            AND sh.start_time >= (SELECT d FROM today)
            AND COALESCE(
              (SELECT COUNT(*) FROM assignments a
               WHERE a.shift_id = sh.id AND a.status = 'confirmed'),
              0
            ) < COALESCE(sh.min_staff, 1)
        ),
        pending_requests AS (
          SELECT COUNT(*) as c
          FROM staffing_requests sr
          WHERE sr.organization_id = $1 AND sr.status = 'open'
        ),
        unassigned_staff AS (
          SELECT COUNT(*) as c
          FROM staff s
          WHERE s.organization_id = $1
            AND s.deleted_at IS NULL
            AND NOT EXISTS (
              SELECT 1 FROM assignments a
              JOIN shifts sh ON sh.id = a.shift_id
              JOIN work_sites ws ON ws.id = sh.site_id
              WHERE a.staff_id = s.id
                AND ws.organization_id = $1
                AND a.status = 'confirmed'
            )
        ),
        recent_confirmed AS (
          SELECT COUNT(*) as c
          FROM assignments a
          JOIN shifts sh ON sh.id = a.shift_id
          JOIN work_sites ws ON ws.id = sh.site_id
          WHERE ws.organization_id = $1
            AND a.status = 'confirmed'
            AND a.confirmed_at >= NOW() - INTERVAL '7 days'
        )
        SELECT
          (SELECT c FROM open_shifts)        AS open_shifts,
          (SELECT c FROM pending_requests)   AS pending_requests,
          (SELECT c FROM unassigned_staff)    AS unassigned_staff,
          (SELECT c FROM recent_confirmed)    AS recent_confirmed""",
        org_id,
    )
    return _json_result({
        "open_shifts": row["open_shifts"],
        "pending_requests": row["pending_requests"],
        "unassigned_staff": row["unassigned_staff"],
        "recent_confirmed": row["recent_confirmed"],
    })