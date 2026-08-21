"""Senzii MCP Server — Python port using the MCP SDK.

Serves the MCP protocol over Streamable HTTP at /mcp.
Auth is via the Authorization: Bearer header (API key or OAuth JWT).

Tools exposed (identical to the Rust MCP server):
- list_staff, get_staff, create_staff, delete_staff
- add_staff_certification, set_staff_availability
- list_clients, create_client
- list_shifts, delete_shift, find_candidates
- list_assignments, create_assignment, unassign_assignment
- list_staffing_requests, create_staffing_request, convert_staffing_request, cancel_staffing_request
- list_work_sites, create_work_site
- list_certifications, create_certification, update_certification, delete_certification
- send_staff_magic_link, send_client_magic_link
- get_org_metrics
- run_sql (admin only, SELECT only)
"""
import base64
import contextlib
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from dotenv import load_dotenv

load_dotenv()

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
    ToolAnnotations,
)
from starlette.responses import JSONResponse
from starlette.routing import Route


# ── Config ───────────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "")
MCP_PORT = int(os.getenv("MCP_PORT", "3001"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "senzii-oauth-secret")
BASE_URL = os.getenv("BASE_URL", "https://senzii.com")

# ── Auth ─────────────────────────────────────────────────────────────────────

def verify_oauth_token(token: str) -> dict | None:
    """Verify a JWT access token (HMAC-SHA256). Returns claims dict or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None

    signing_input = f"{parts[0]}.{parts[1]}"
    expected_sig = hmac.new(
        SESSION_SECRET.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()

    if not hmac.compare_digest(expected_b64, parts[2]):
        return None

    try:
        payload = base64.urlsafe_b64decode(parts[1] + "==")
        claims = json.loads(payload)
    except Exception:
        return None

    exp = claims.get("exp")
    if exp is None or datetime.now(timezone.utc).timestamp() > exp:
        return None

    org_id = claims.get("org_id")
    if org_id is None:
        return None

    return {
        "org_id": int(org_id),
        "role": claims.get("role", "admin"),
        "entity_id": claims.get("entity_id"),
        "user_id": claims.get("user_id"),
    }


# ── DB helpers ───────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    return _pool


# ── Server state ─────────────────────────────────────────────────────────────

class ServerState:
    """Per-request state — holds the resolved org_id, role, and user_id."""
    org_id: int = 0
    role: str = "admin"
    entity_id: int | None = None
    user_id: int | None = None

    @classmethod
    def from_claims(cls, claims: dict):
        s = cls()
        s.org_id = claims.get("org_id", 0)
        s.role = claims.get("role", "admin")
        s.entity_id = claims.get("entity_id")
        s.user_id = claims.get("user_id")
        return s

    def is_admin(self) -> bool:
        return self.role in ("admin", "super")


# Global state — overwritten per request
_state = ServerState()


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


# ── Server ────────────────────────────────────────────────────────────────────

server = Server("senzii")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return all available tools (same as the Rust MCP server)."""
    tools = [
        Tool(name="list_staff", description="List all staff members. Use this to find a staff_id before calling get_staff, set_staff_availability, add_staff_certification, or delete_staff.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_staff", description="Get a single staff member by ID, including their certifications and weekly availability.", inputSchema={"type": "object", "properties": {"id": {"type": "integer", "description": "Staff member ID"}}, "required": ["id"]}),
        Tool(name="create_staff", description="Create a new staff member with optional certifications. Call list_certifications first to get valid certification names.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}, "address": {"type": "string"}, "latitude": {"type": "number"}, "longitude": {"type": "number"}, "timezone": {"type": "string"}, "certifications": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "expires_at": {"type": "string"}}, "required": ["name"]}}}, "required": ["name", "email", "timezone"]}),
        Tool(name="add_staff_certification", description="Add a certification to an existing staff member. The certification name must already exist at the organization level.", inputSchema={"type": "object", "properties": {"staff_id": {"type": "integer"}, "certification": {"type": "object", "properties": {"name": {"type": "string"}, "expires_at": {"type": "string"}}, "required": ["name"]}}, "required": ["staff_id", "certification"]}),
        Tool(name="set_staff_availability", description="Set a staff member's weekly availability schedule. This REPLACES all existing availability.", inputSchema={"type": "object", "properties": {"staff_id": {"type": "integer"}, "windows": {"type": "array", "items": {"type": "object", "properties": {"day_of_week": {"type": "integer"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}}, "required": ["day_of_week", "start_time", "end_time"]}}}, "required": ["staff_id", "windows"]}),
        Tool(name="list_clients", description="List all clients. Use this to find a client_id before calling create_staffing_request or send_client_magic_link.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="create_client", description="Create a new client (the company that requests staff).", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}, "company_name": {"type": "string"}}, "required": ["name", "email"]}),
        Tool(name="list_shifts", description="List all shifts, optionally filtered by work site.", inputSchema={"type": "object", "properties": {"site_id": {"type": "integer"}}}),
        Tool(name="delete_shift", description="Delete (soft delete) a shift. Blocks if the shift has confirmed assignments.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="find_candidates", description="Find ranked staff candidates for a shift using the matching engine. Scores each staff member on proximity (30%), skills match (40%), and availability (30%).", inputSchema={"type": "object", "properties": {"shift_id": {"type": "integer"}}, "required": ["shift_id"]}),
        Tool(name="list_assignments", description="List all assignments, optionally filtered by shift.", inputSchema={"type": "object", "properties": {"shift_id": {"type": "integer"}}}),
        Tool(name="create_assignment", description="Assign a staff member to a shift. Creates a pending assignment.", inputSchema={"type": "object", "properties": {"shift_id": {"type": "integer"}, "staff_id": {"type": "integer"}}, "required": ["shift_id", "staff_id"]}),
        Tool(name="unassign_assignment", description="Remove (delete) an assignment — unassigns the staff member from the shift.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="list_staffing_requests", description="List staffing requests, optionally filtered by status: open, filled, or cancelled.", inputSchema={"type": "object", "properties": {"status": {"type": "string"}}}),
        Tool(name="create_staffing_request", description="Create a new staffing request — a client asks for staff for a specific date/time.", inputSchema={"type": "object", "properties": {"client_id": {"type": "integer"}, "site_id": {"type": "integer"}, "shift_date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}, "required_skills": {"type": "array", "items": {"type": "string"}}, "min_staff": {"type": "integer"}, "notes": {"type": "string"}}, "required": ["client_id", "shift_date", "start_time", "end_time"]}),
        Tool(name="convert_staffing_request", description="Convert an accepted staffing request into a shift on the schedule board.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="cancel_staffing_request", description="Cancel a staffing request. If the request was already accepted, the linked shift is deleted and staff are unassigned.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="list_work_sites", description="List all work sites.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="create_work_site", description="Create a new work site (a location where shifts happen).", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "address": {"type": "string"}, "latitude": {"type": "number"}, "longitude": {"type": "number"}, "required_skills": {"type": "array", "items": {"type": "string"}}, "timezone": {"type": "string"}}, "required": ["name"]}),
        Tool(name="list_certifications", description="List all organization-level certifications. Use this to get valid certification names before creating staff with certifications.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="create_certification", description="Add a certification to the org if it doesn't already exist. Idempotent.", inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
        Tool(name="update_certification", description="Rename an organization-level certification.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}, "required": ["id", "name"]}),
        Tool(name="delete_certification", description="Delete an organization-level certification. This also removes it from any staff who have it assigned.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="delete_staff", description="Archive (soft delete) a staff member. Blocks if the staff has upcoming confirmed assignments.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="send_staff_magic_link", description="Generate and return a magic login link for a staff member.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="send_client_magic_link", description="Generate and return a magic login link for a client.", inputSchema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
        Tool(name="get_org_metrics", description="Get dashboard metrics: open shift count, pending request count, unassigned staff count, recent confirmed assignments.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="run_sql", description="Execute a read-only SELECT query for ad-hoc reporting. Admin only. Do NOT use for data that dedicated tools handle.", inputSchema={"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}, annotations=ToolAnnotations(readOnlyHint=True)),
    ]
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    """Handle tool calls — routes to the appropriate DB function."""
    pool = await get_pool()
    org_id = _state.org_id
    user_id = _state.user_id
    role = _state.role

    async with pool.acquire() as conn:
        try:
            if name == "list_staff":
                rows = await conn.fetch(
                    """SELECT s.*, COALESCE(cert.certs, '[]') as certifications
                       FROM staff s
                       LEFT JOIN LATERAL (
                         SELECT jsonb_agg(jsonb_build_object('name', name, 'expires_at', expires_at)) as certs
                         FROM staff_certifications WHERE staff_id = s.id
                       ) cert ON true
                       WHERE s.organization_id = $1 AND s.deleted_at IS NULL
                       ORDER BY s.id""",
                    org_id,
                )
                out = []
                for r in rows:
                    d = dict(r)
                    # asyncpg returns JSONB as a string — decode to match Rust contract
                    try:
                        d["certifications"] = json.loads(d["certifications"]) if isinstance(d["certifications"], str) else d["certifications"]
                    except Exception:
                        d["certifications"] = []
                    out.append(d)
                return _json_result(out)

            elif name == "get_staff":
                row = await conn.fetchrow(
                    """SELECT s.*, COALESCE(cert.certs, '[]') as certifications,
                             COALESCE(avail.windows, '[]') as availability
                       FROM staff s
                       LEFT JOIN LATERAL (
                         SELECT jsonb_agg(jsonb_build_object('name', name, 'expires_at', expires_at)) as certs
                         FROM staff_certifications WHERE staff_id = s.id
                       ) cert ON true
                       LEFT JOIN LATERAL (
                         SELECT jsonb_agg(jsonb_build_object('day_of_week', day_of_week, 'start_time', start_time, 'end_time', end_time)) as windows
                         FROM staff_availability WHERE staff_id = s.id
                       ) avail ON true
                       WHERE s.id = $1 AND s.organization_id = $2 AND s.deleted_at IS NULL""",
                    arguments["id"], org_id,
                )
                if row is None:
                    return _error_result("Staff member not found")
                d = dict(row)
                for key in ("certifications", "availability"):
                    try:
                        d[key] = json.loads(d[key]) if isinstance(d[key], str) else d[key]
                    except Exception:
                        d[key] = []
                return _json_result(d)

            elif name == "create_staff":
                # Geocode address if provided
                lat = arguments.get("latitude")
                lng = arguments.get("longitude")
                address = arguments.get("address")
                if address and lat is None:
                    from app.services.geocode import geocode_address
                    coords = await geocode_address(address)
                    if coords:
                        lat = coords.latitude
                        lng = coords.longitude

                row = await conn.fetchrow(
                    """INSERT INTO staff (name, email, phone, address, latitude, longitude, timezone, organization_id)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *""",
                    arguments["name"], arguments["email"], arguments.get("phone"),
                    address, lat, lng, arguments.get("timezone", "UTC"), org_id,
                )
                staff_id = row["id"]

                # Add certifications if provided
                certs_out = []
                for cert in arguments.get("certifications", []):
                    cert_row = await conn.fetchrow(
                        "SELECT id FROM organization_certifications WHERE organization_id = $1 AND name = $2",
                        org_id, cert["name"],
                    )
                    if cert_row:
                        expires_at = None
                        if cert.get("expires_at"):
                            try:
                                expires_at = datetime.fromisoformat(cert["expires_at"].replace("Z", "+00:00"))
                            except Exception:
                                pass
                        await conn.execute(
                            "INSERT INTO staff_certifications (staff_id, name, expires_at, cert_id) VALUES ($1, $2, $3, $4)",
                            staff_id, cert["name"], expires_at, cert_row["id"],
                        )
                        certs_out.append({"name": cert["name"], "expires_at": cert.get("expires_at")})

                result = dict(row)
                result["certifications"] = certs_out
                await _audit_log(conn, org_id, user_id, "staff.create", "staff", staff_id, None, dict(row))
                return _json_result(result)

            elif name == "add_staff_certification":
                staff_id = arguments["staff_id"]
                cert_input = arguments["certification"]
                cert_row = await conn.fetchrow(
                    "SELECT id FROM organization_certifications WHERE organization_id = $1 AND name = $2",
                    org_id, cert_input["name"],
                )
                if cert_row is None:
                    return _error_result("Unknown certification. Add it to your org's certification list first.")

                expires_at = None
                if cert_input.get("expires_at"):
                    try:
                        expires_at = datetime.fromisoformat(cert_input["expires_at"].replace("Z", "+00:00"))
                    except Exception:
                        pass

                row = await conn.fetchrow(
                    "INSERT INTO staff_certifications (staff_id, name, expires_at, cert_id) VALUES ($1, $2, $3, $4) RETURNING *",
                    staff_id, cert_input["name"], expires_at, cert_row["id"],
                )
                await _audit_log(conn, org_id, user_id, "staff_certification.add", "staff_certification", row["id"], None, dict(row))
                return _json_result(dict(row))

            elif name == "set_staff_availability":
                staff_id = arguments["staff_id"]
                # Delete existing availability
                await conn.execute("DELETE FROM staff_availability WHERE staff_id = $1", staff_id)
                # Insert new windows
                for w in arguments["windows"]:
                    await conn.execute(
                        "INSERT INTO staff_availability (staff_id, day_of_week, start_time, end_time) VALUES ($1, $2, $3, $4)",
                        staff_id, w["day_of_week"], w["start_time"], w["end_time"],
                    )
                await _audit_log(conn, org_id, user_id, "staff_availability.set", "staff", staff_id, None, {"windows": arguments["windows"]})
                return _json_result({"ok": True, "windows": arguments["windows"]})

            elif name == "list_clients":
                rows = await conn.fetch(
                    "SELECT * FROM clients WHERE organization_id = $1 ORDER BY id", org_id)
                return _json_result([dict(r) for r in rows])

            elif name == "create_client":
                row = await conn.fetchrow(
                    """INSERT INTO clients (name, email, phone, company_name, organization_id)
                       VALUES ($1, $2, $3, $4, $5) RETURNING *""",
                    arguments["name"], arguments["email"], arguments.get("phone"),
                    arguments.get("company_name"), org_id,
                )
                await _audit_log(conn, org_id, user_id, "client.create", "client", row["id"], None, dict(row))
                return _json_result(dict(row))

            elif name == "list_shifts":
                if arguments.get("site_id"):
                    rows = await conn.fetch(
                        """SELECT s.*, ws.name as site_name FROM shifts s
                           JOIN work_sites ws ON ws.id = s.site_id
                           WHERE s.organization_id = $1 AND s.deleted_at IS NULL AND s.site_id = $2
                           ORDER BY s.start_time""",
                        org_id, arguments["site_id"],
                    )
                else:
                    rows = await conn.fetch(
                        """SELECT s.*, ws.name as site_name FROM shifts s
                           JOIN work_sites ws ON ws.id = s.site_id
                           WHERE s.organization_id = $1 AND s.deleted_at IS NULL
                           ORDER BY s.start_time""",
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

            elif name == "delete_shift":
                shift_id = arguments["id"]
                # Check for confirmed assignments
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM assignments WHERE shift_id = $1 AND organization_id = $2 AND status = 'confirmed'",
                    shift_id, org_id,
                )
                if count > 0:
                    return _error_result(f"Cannot delete shift — {count} confirmed assignment(s). Remove or reassign staff first.")
                await conn.execute(
                    "UPDATE shifts SET deleted_at = NOW(), assigned_staff_id = NULL WHERE id = $1 AND organization_id = $2",
                    shift_id, org_id,
                )
                await conn.execute(
                    "UPDATE assignments SET status = 'rejected' WHERE shift_id = $1 AND organization_id = $2 AND status = 'pending'",
                    shift_id, org_id,
                )
                await _audit_log(conn, org_id, user_id, "shift.delete", "shift", shift_id, None, None)
                return _json_result({"ok": True, "message": "Shift deleted"})

            elif name == "find_candidates":
                from app.db.shifts import find_matching_staff
                candidates = await find_matching_staff(conn, arguments["shift_id"], org_id, 0.3, 0.4, 0.3)
                if candidates is None:
                    return _error_result("Shift not found")
                return _json_result(candidates)

            elif name == "list_assignments":
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

            elif name == "create_assignment":
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

            elif name == "unassign_assignment":
                await conn.execute("DELETE FROM assignments WHERE id = $1", arguments["id"])
                await _audit_log(conn, org_id, user_id, "assignment.delete", "assignment", arguments["id"], None, None)
                return _json_result({"ok": True, "message": "Assignment removed"})

            elif name == "list_staffing_requests":
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

            elif name == "create_staffing_request":
                # Port of mcp_server/src/db/staffing_requests.rs create_staffing_request
                # asyncpg requires real date/time objects, not strings.
                from datetime import date, time as dtime
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

            elif name == "convert_staffing_request":
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

            elif name == "cancel_staffing_request":
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

            elif name == "list_work_sites":
                rows = await conn.fetch(
                    "SELECT * FROM work_sites WHERE organization_id = $1 ORDER BY name", org_id)
                return _json_result([dict(r) for r in rows])

            elif name == "create_work_site":
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

            elif name == "list_certifications":
                rows = await conn.fetch(
                    "SELECT * FROM organization_certifications WHERE organization_id = $1 ORDER BY name", org_id)
                return _json_result([dict(r) for r in rows])

            elif name == "create_certification":
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

            elif name == "update_certification":
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

            elif name == "delete_certification":
                await conn.execute(
                    "DELETE FROM organization_certifications WHERE id = $1 AND organization_id = $2",
                    arguments["id"], org_id,
                )
                await _audit_log(conn, org_id, user_id, "org_certification.delete", "organization_certification", arguments["id"], None, None)
                return _json_result({"ok": True, "message": "Certification deleted"})

            elif name == "delete_staff":
                staff_id = arguments["id"]
                count = await conn.fetchval(
                    """SELECT COUNT(*) FROM assignments a
                       JOIN shifts sh ON sh.id = a.shift_id
                       WHERE a.staff_id = $1 AND a.organization_id = $2
                       AND a.status = 'confirmed' AND sh.end_time > NOW()""",
                    staff_id, org_id,
                )
                if count > 0:
                    return _error_result(f"Cannot delete staff — {count} active assignment(s). Remove or reassign first.")
                await conn.execute(
                    "UPDATE staff SET deleted_at = NOW() WHERE id = $1 AND organization_id = $2",
                    staff_id, org_id,
                )
                await _audit_log(conn, org_id, user_id, "staff.delete", "staff", staff_id, None, None)
                return _json_result({"ok": True, "message": "Staff member archived"})

            elif name == "send_staff_magic_link":
                staff_id = arguments["id"]
                # Verify staff exists and belongs to this org (Rust contract)
                staff_row = await conn.fetchrow(
                    "SELECT id, email, name FROM staff WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL",
                    staff_id, org_id,
                )
                if staff_row is None:
                    return _error_result("Staff member not found")
                token = str(uuid.uuid4())
                row = await conn.fetchrow(
                    "INSERT INTO magic_tokens (staff_id, token, expires_at) VALUES ($1, $2, NOW() + INTERVAL '7 days') RETURNING token, expires_at",
                    staff_id, token,
                )
                link = f"{BASE_URL}/staff/login?token={row['token']}"
                return _json_result({
                    "token": row["token"],
                    "expires_at": row["expires_at"],
                    "staff_email": staff_row["email"],
                    "staff_name": staff_row["name"],
                    "link": link,
                })

            elif name == "send_client_magic_link":
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

            elif name == "get_org_metrics":
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

            elif name == "run_sql":
                if not _state.is_admin():
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

            else:
                return _error_result(f"Unknown tool: {name}")

        except Exception as e:
            return _error_result(str(e))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    """Run the MCP server with Streamable HTTP transport."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Health check bypasses auth so Fly's health checks succeed.
            if request.url.path == "/health":
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else auth
            if not token:
                from starlette.responses import Response
                return Response(
                    "Missing Authorization header",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer resource_metadata="https://senzii.com/.well-known/oauth-protected-resource"'},
                )

            # Try OAuth JWT first
            claims = verify_oauth_token(token)
            if claims:
                global _state
                _state = ServerState.from_claims(claims)
                return await call_next(request)

            # Fall back to API key
            pool = await get_pool()
            async with pool.acquire() as conn:
                from mcp_server.api_keys import verify_api_key
                result = await verify_api_key(conn, token)
                if result:
                    _state = ServerState()
                    _state.org_id = result[1]
                    _state.user_id = result[0]
                    _state.role = "admin"
                    return await call_next(request)

            from starlette.responses import Response
            return Response(
                "Invalid or revoked token",
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://senzii.com/.well-known/oauth-protected-resource"'},
            )

    # Create the streamable HTTP app
    session_manager = StreamableHTTPSessionManager(app=server)

    async def asgi_app(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        # SDK 1.28+ requires the session manager to run inside a task group
        # (StreamableHTTPSessionManager.run()). Without this, every request
        # 500s with "Task group is not initialized. Make sure to use run()."
        async with session_manager.run():
            yield

    app = Starlette(
        routes=[
            # Health first so it isn't shadowed by the root mount below.
            Route("/health", lambda request: JSONResponse({"status": "healthy"})),
            # Mount at "/" — this MCP process only receives mcp.senzii.com
            # traffic (Caddy routes it), and a root mount means /mcp matches
            # directly: no Starlette 307 /mcp -> /mcp/ slash redirect, which
            # would downgrade to http:// behind the TLS-terminating LB and
            # break urllib-based clients (e2e_mcp_test.py doesn't follow
            # POST redirects).
            Mount("/", app=asgi_app),
        ],
        middleware=[Middleware(AuthMiddleware)],
        lifespan=lifespan,
    )

    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)


if __name__ == "__main__":
    main()