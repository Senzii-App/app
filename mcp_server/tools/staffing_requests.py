"""Staffing request MCP tools — list, create, convert, cancel staffing requests."""
import json
from datetime import datetime

import asyncpg
from mcp.types import CallToolResult

from mcp_server.tools.common import _audit_log, _error_result, _json_result


async def list_staffing_requests(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    if arguments.get("status"):
        rows = await conn.fetch(
            """SELECT sr.*, c.name as client_name, ws.name as site_name
               FROM staffing_requests sr
               JOIN clients c ON c.id = sr.client_id
               LEFT JOIN work_sites ws ON ws.id = sr.site_id
               WHERE sr.organization_id = $1 AND sr.status = $2 ORDER BY sr.id""",
            org_id, arguments["status"],
        )
    else:
        rows = await conn.fetch(
            """SELECT sr.*, c.name as client_name, ws.name as site_name
               FROM staffing_requests sr
               JOIN clients c ON c.id = sr.client_id
               LEFT JOIN work_sites ws ON ws.id = sr.site_id
               WHERE sr.organization_id = $1 ORDER BY sr.id""",
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


async def create_staffing_request(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    # Port of mcp_server/src/db/staffing_requests.rs create_staffing_request
    # asyncpg requires real date/time objects, not strings.
    shift_date = datetime.strptime(arguments["shift_date"], "%Y-%m-%d").date()
    start_time = datetime.strptime(arguments["start_time"], "%H:%M").time()
    end_time = datetime.strptime(arguments["end_time"], "%H:%M").time()
    skills_json = json.dumps(arguments.get("required_skills", []) or [])
    min_staff = arguments.get("min_staff") or 1
    row = await conn.fetchrow(
        """INSERT INTO staffing_requests
           (organization_id, client_id, site_id, shift_date, start_time, end_time,
            required_skills, min_staff, notes, status)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, 'open')
           RETURNING id, organization_id, client_id, site_id, shift_date,
                     start_time, end_time, required_skills, min_staff, notes, status""",
        org_id, arguments["client_id"], arguments["site_id"],
        shift_date, start_time, end_time,
        skills_json, min_staff, arguments.get("notes"),
    )
    result = dict(row)
    # asyncpg returns JSONB as string — decode to match Rust contract
    try:
        result["required_skills"] = json.loads(result["required_skills"]) if isinstance(result["required_skills"], str) else result["required_skills"]
    except Exception:
        result["required_skills"] = []
    await _audit_log(conn, org_id, user_id, "staffing_request.create",
                     "staffing_request", row["id"], None, dict(row))
    return _json_result(result)


async def convert_staffing_request(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    req_id = arguments["id"]
    req = await conn.fetchrow(
        "SELECT * FROM staffing_requests WHERE id = $1 AND organization_id = $2",
        req_id, org_id,
    )
    if req is None:
        return _error_result("Staffing request not found")
    if req["status"] not in ("accepted", "open"):
        return _error_result(f"Request is in '{req['status']}' state — only accepted requests can be converted")

    # Create a shift from the request
    row = await conn.fetchrow(
        """INSERT INTO shifts (organization_id, site_id, start_time, end_time, required_skills, min_staff, request_id)
           SELECT $1, site_id,
             (shift_date::timestamp + start_time),
             (shift_date::timestamp + end_time),
             required_skills, min_staff, id
           FROM staffing_requests WHERE id = $2 AND organization_id = $1
           RETURNING id""",
        org_id, req_id,
    )
    shift_id = row["id"]
    # Mark request as accepted (or keep accepted)
    await conn.execute(
        "UPDATE staffing_requests SET status = 'accepted', updated_at = NOW() WHERE id = $1",
        req_id,
    )
    await _audit_log(conn, org_id, user_id, "staffing_request.convert", "staffing_request", req_id, None, {"shift_id": shift_id})
    # Rust contract: ConvertResult { shift_id, message }
    return _json_result({
        "shift_id": shift_id,
        "message": "Staffing request converted to shift",
    })


async def cancel_staffing_request(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    req_id = arguments["id"]
    req = await conn.fetchrow(
        "SELECT * FROM staffing_requests WHERE id = $1 AND organization_id = $2",
        req_id, org_id,
    )
    if req is None:
        return _error_result("Staffing request not found")

    # If linked shift exists, delete it
    if req.get("site_id"):
        linked = await conn.fetch(
            "SELECT id FROM shifts WHERE request_id = $1 AND organization_id = $2 AND deleted_at IS NULL",
            req_id, org_id,
        )
        for shift_row in linked:
            confirmed = await conn.fetchval(
                "SELECT COUNT(*) FROM assignments WHERE shift_id = $1 AND status = 'confirmed'",
                shift_row["id"],
            )
            if confirmed > 0:
                return _error_result(f"Cannot cancel — linked shift has {confirmed} confirmed assignment(s)")
            await conn.execute("UPDATE shifts SET deleted_at = NOW(), assigned_staff_id = NULL WHERE id = $1", shift_row["id"])

    await conn.execute("UPDATE staffing_requests SET status = 'cancelled', updated_at = NOW() WHERE id = $1", req_id)
    await _audit_log(conn, org_id, user_id, "staffing_request.cancel", "staffing_request", req_id, None, None)
    return _json_result({"ok": True, "message": "Staffing request cancelled"})