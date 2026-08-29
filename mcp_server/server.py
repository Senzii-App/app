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
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv

load_dotenv()

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolResult,
    Tool,
    ToolAnnotations,
)
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_server.config import DATABASE_URL, MCP_PORT, SESSION_SECRET
from mcp_server.tools import TOOL_HANDLERS
from mcp_server.tools.common import _error_result


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
    """Handle tool calls — dispatches to the tool function from TOOL_HANDLERS."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _error_result(f"Unknown tool: {name}")

    pool = await get_pool()
    org_id = _state.org_id
    user_id = _state.user_id
    role = _state.role

    async with pool.acquire() as conn:
        try:
            return await handler(conn, org_id, user_id, role, arguments)
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