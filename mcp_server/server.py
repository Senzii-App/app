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
from mcp.server.lowlevel.server import request_ctx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolResult,
    Tool,
    ToolAnnotations,
)
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_server.config import DATABASE_URL, MCP_PORT, SESSION_SECRET
from mcp_server.tools import TOOL_HANDLERS, load_tools
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


# Request-scoped identity.
#
# The identity is carried on request.scope (set by AuthMiddleware after token
# verification) and read per message via the SDK's request_ctx — the MCP
# server hands each JSON-RPC message its originating HTTP request, including
# on the SSE path. An earlier revision used a ContextVar, but in stateful
# sessions tool handlers run in a task spawned once at session creation, so
# the ContextVar held the initialize-time identity for the session's whole
# lifetime (cross-credential use of a session id, and revocation, were not
# reflected). request_ctx is set per message, so identity follows the
# credential on every call and revocation takes effect immediately.
_STATE_SCOPE_KEY = "senzii_identity"


# ── Server ────────────────────────────────────────────────────────────────────

server = Server("senzii")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return all available tools (same as the Rust MCP server)."""
    return load_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    """Handle tool calls — dispatches to the tool function from TOOL_HANDLERS."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _error_result(f"Unknown tool: {name}")

    try:
        state = request_ctx.get().request.scope[_STATE_SCOPE_KEY]
    except LookupError:
        # No request context (e.g. tool invoked outside a message handler) —
        # refuse rather than fall back to a default (which would otherwise
        # run as org 0 / admin).
        return _error_result("Unauthenticated: no request identity bound")
    except (AttributeError, KeyError):
        # No identity was bound to this request — the auth middleware did not
        # run (e.g. a path that bypasses it) — refuse rather than fall back.
        return _error_result("Unauthenticated: no request identity bound")

    pool = await get_pool()
    org_id = state.org_id
    user_id = state.user_id
    role = state.role

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
                request.scope[_STATE_SCOPE_KEY] = ServerState.from_claims(claims)
                return await call_next(request)

            # Fall back to API key
            pool = await get_pool()
            async with pool.acquire() as conn:
                from mcp_server.api_keys import verify_api_key
                result = await verify_api_key(conn, token)
                if result:
                    state = ServerState()
                    state.org_id = result[1]
                    state.user_id = result[0]
                    state.role = "admin"
                    request.scope[_STATE_SCOPE_KEY] = state
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