"""OAuth 2.1 authorization server routes for MCP clients.

Direct port of src/routes/oauth.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Implements the endpoints MCP clients (Claude Desktop, ChatGPT, Hermes)
discover and call:

  GET  /.well-known/oauth-authorization-server  → metadata discovery (RFC 8414)
  GET  /.well-known/oauth-protected-resource    → resource metadata (RFC 9728)
  GET  /oauth/register                          → registration info / form
  POST /oauth/register                           → dynamic client registration (RFC 7591)
  GET  /oauth/authorize                          → authorization (user login + consent)
  POST /oauth/authorize                          → consent form submission
  POST /oauth/token                             → token exchange (auth code + refresh)
  GET  /oauth/me                                 → current user info from session

The flow:
  1. Client hits the MCP server → gets 401 with WWW-Authenticate header pointing
     to the protected resource metadata URL.
  2. Client fetches /.well-known/oauth-authorization-server → discovers endpoints.
  3. Client POSTs to /oauth/register → gets client_id (auto-registered).
  4. Client redirects user to /oauth/authorize?client_id=...&redirect_uri=...&code_challenge=...
  5. User is already logged into Senzii (session cookie) → sees consent screen → "Allow".
  6. Server redirects back to client's redirect_uri with ?code=...
  7. Client POSTs to /oauth/token with code + code_verifier → gets JWT access token.
  8. Client sends Authorization: Bearer *** on every MCP request.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.db.pool import get_pool

router = APIRouter()


# ── Session keys (reused from auth.rs) ────────────────────────────────────────

USER_ID_KEY = "userId"
ORG_ID_KEY = "organizationId"
USER_EMAIL_KEY = "userEmail"
ROLE_KEY = "role"
STAFF_ID_KEY = "staffId"
CLIENT_ID_KEY = "clientId"


# ── Helpers ──────────────────────────────────────────────────────────────────


def url_encode(s: str) -> str:
    """URL-encode a string (mirrors the Rust url_encode helper)."""
    out = []
    for c in s:
        if c == "@":
            out.append("%40")
        elif c == ".":
            out.append("%2E")
        elif c == "+":
            out.append("%2B")
        elif c == " ":
            out.append("%20")
        elif c == "/":
            out.append("%2F")
        elif c == "?":
            out.append("%3F")
        elif c == "&":
            out.append("%26")
        elif c == "=":
            out.append("%3D")
        elif c == ":":
            out.append("%3A")
        elif c.isascii() and (c.isalnum() or c in "-_"):
            out.append(c)
        else:
            out.append(f"%{ord(c):02X}")
    return "".join(out)


def base64url_encode(data: bytes) -> str:
    """Base64url encode without padding (RFC 7636 §4.2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def base64url_decode(s: str) -> bytes:
    """Decode base64url (no padding) to bytes."""
    padding = 4 - (len(s) % 4)
    if padding != 4:
        s = s + ("=" * padding)
    return base64.urlsafe_b64decode(s)


def verify_pkce(verifier: str, challenge: str) -> bool:
    """Verify a PKCE code challenge against the provided verifier.

    SHA-256 method: BASE64URL(SHA256(verifier)) == challenge
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64url_encode(digest) == challenge


def create_jwt(claims: dict, secret: str) -> str:
    """Create a signed JWT using HS256."""
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = base64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    sig_b64 = base64url_encode(sig)
    return f"{signing_input}.{sig_b64}"


def verify_jwt(token: str, secret: str) -> dict | None:
    """Verify a JWT signature and return the claims. Returns None if invalid."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    signing_input = f"{parts[0]}.{parts[1]}"
    expected_sig = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    expected_b64 = base64url_encode(expected_sig)
    if expected_b64 != parts[2]:
        return None
    try:
        payload_bytes = base64url_decode(parts[1])
        return json.loads(payload_bytes)
    except Exception:
        return None


def _token_hash(token: str) -> str:
    """SHA-256 hex hash of a token (for DB storage / revocation lookup)."""
    return hashlib.sha256(token.encode()).hexdigest()


# ── DB helpers (mirror db::oauth in Rust) ─────────────────────────────────────


async def db_register_client(conn, client_name: str, redirect_uris: list[str]) -> dict:
    """Register a new OAuth client (dynamic client registration, RFC 7591)."""
    client_id = f"senzii_oauth_{uuid.uuid4().hex}"
    client_secret = secrets.token_hex(32)
    await conn.execute(
        "INSERT INTO oauth_clients (client_id, client_secret, client_name, redirect_uris) "
        "VALUES ($1, $2, $3, $4)",
        client_id, client_secret, client_name, redirect_uris,
    )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "created_at": datetime.now(timezone.utc),
    }


async def db_get_client(conn, client_id: str) -> dict | None:
    """Look up an OAuth client by ID."""
    row = await conn.fetchrow(
        "SELECT client_id, client_secret, client_name, redirect_uris, created_at "
        "FROM oauth_clients WHERE client_id = $1",
        client_id,
    )
    if row is None:
        return None
    return {
        "client_id": row["client_id"],
        "client_secret": row["client_secret"],
        "client_name": row["client_name"],
        "redirect_uris": row["redirect_uris"],
        "created_at": row["created_at"],
    }


async def db_create_authorization_code(
    conn, code, client_id, user_id, org_id, role, entity_id,
    redirect_uri, code_challenge, scope,
):
    """Create an authorization code (short-lived, ~10 min)."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await conn.execute(
        """INSERT INTO oauth_authorization_codes
           (code, client_id, user_id, organization_id, role, entity_id,
            redirect_uri, code_challenge, scope, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
        code, client_id, user_id, org_id, role, entity_id,
        redirect_uri, code_challenge, scope, expires_at,
    )


async def db_consume_authorization_code(conn, code: str) -> dict | None:
    """Consume an authorization code (single-use). Returns the associated data if valid."""
    row = await conn.fetchrow(
        """DELETE FROM oauth_authorization_codes
           WHERE code = $1 AND expires_at > now()
           RETURNING client_id, user_id, organization_id, role, entity_id,
                     redirect_uri, code_challenge, scope""",
        code,
    )
    if row is None:
        return None
    return {
        "client_id": row["client_id"],
        "user_id": row["user_id"],
        "organization_id": row["organization_id"],
        "role": row["role"],
        "entity_id": row["entity_id"],
        "redirect_uri": row["redirect_uri"],
        "code_challenge": row["code_challenge"],
        "scope": row["scope"],
    }


async def db_store_token(conn, token, user_id, org_id, oauth_client_id, expires_at):
    """Store a token hash (for revocation tracking). We hash the JWT so the DB
    doesn't store raw tokens."""
    token_hash = _token_hash(token)
    await conn.execute(
        """INSERT INTO oauth_tokens
           (token_hash, user_id, organization_id, client_id, expires_at)
           VALUES ($1, $2, $3, $4, $5)""",
        token_hash, user_id, org_id, oauth_client_id, expires_at,
    )


async def db_create_refresh_token(
    conn, user_id, org_id, oauth_client_id, role, entity_id, scope,
) -> str:
    """Create a refresh token and store its hash. Returns the raw token string.
    Refresh tokens are long-lived (default 1 year) and rotate on each use."""
    raw_token = f"rt_{uuid.uuid4().hex}"
    token_hash = _token_hash(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=365)
    await conn.execute(
        """INSERT INTO oauth_refresh_tokens
           (refresh_token_hash, user_id, organization_id, client_id, role,
            entity_id, scope, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
        token_hash, user_id, org_id, oauth_client_id, role, entity_id,
        scope, expires_at,
    )
    return raw_token


async def db_consume_refresh_token(conn, raw_token: str) -> dict | None:
    """Consume a refresh token (single-use rotation). Returns the associated data
    if valid and not expired/revoked, and deletes the consumed token.
    The caller is responsible for issuing a new refresh token."""
    token_hash = _token_hash(raw_token)
    row = await conn.fetchrow(
        """DELETE FROM oauth_refresh_tokens
           WHERE refresh_token_hash = $1
             AND expires_at > now()
             AND revoked_at IS NULL
           RETURNING user_id, organization_id, client_id, role, entity_id, scope""",
        token_hash,
    )
    if row is None:
        return None
    return {
        "user_id": row["user_id"],
        "organization_id": row["organization_id"],
        "client_id": row["client_id"],
        "role": row["role"],
        "entity_id": row["entity_id"],
        "scope": row["scope"],
    }


# ── Discovery endpoints ──────────────────────────────────────────────────────


@router.get("/.well-known/oauth-authorization-server")
async def discovery():
    """GET /.well-known/oauth-authorization-server

    Returns OAuth 2.1 authorization server metadata (RFC 8414).
    MCP clients use this to discover the authorize, token, and register endpoints.
    """
    return JSONResponse(
        {
            "issuer": "https://senzii.com",
            "authorization_endpoint": "https://senzii.com/oauth/authorize",
            "token_endpoint": "https://senzii.com/oauth/token",
            "registration_endpoint": "https://senzii.com/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "scopes_supported": ["mcp:tools"],
        }
    )


@router.get("/.well-known/oauth-protected-resource")
async def resource_metadata():
    """GET /.well-known/oauth-protected-resource

    Returns protected resource metadata (RFC 9728). MCP clients check this
    to find the authorization server that protects the MCP endpoint.
    """
    return JSONResponse(
        {
            "resource": "https://senzii.com/mcp",
            "authorization_servers": ["https://senzii.com"],
            "scopes_supported": ["mcp:tools"],
            "bearer_methods_supported": ["header"],
        }
    )


# ── Dynamic client registration ──────────────────────────────────────────────


class RegisterRequest(BaseModel):
    client_name: str | None = None
    redirect_uris: list[str]
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    token_endpoint_auth_method: str | None = None


@router.get("/oauth/register")
async def register_get():
    """GET /oauth/register — registration info.

    Returns a simple info response describing how to register an MCP client.
    """
    return JSONResponse(
        {
            "message": "POST to this endpoint with client_name and redirect_uris to register a new MCP client.",
            "required_fields": ["redirect_uris"],
            "optional_fields": ["client_name", "grant_types", "response_types", "token_endpoint_auth_method"],
        }
    )


@router.post("/oauth/register")
async def register(req: RegisterRequest):
    """POST /oauth/register — dynamic client registration (RFC 7591)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        name = req.client_name or "MCP Client"
        try:
            client = await db_register_client(conn, name, req.redirect_uris)
        except Exception as e:
            return JSONResponse(
                {"error": "server_error", "error_description": str(e)},
                status_code=500,
            )

    return JSONResponse(
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "client_name": client["client_name"],
            "redirect_uris": client["redirect_uris"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


# ── Authorization endpoint ────────────────────────────────────────────────────


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    state: str | None = None,
    scope: str | None = None,
):
    """GET /oauth/authorize

    If the user is logged in (has a Senzii session), show a consent screen.
    If not logged in, redirect to the login page with a return URL.
    """
    session = request.session
    user_id = session.get(USER_ID_KEY)
    org_id = session.get(ORG_ID_KEY)
    email = session.get(USER_EMAIL_KEY)
    role = session.get(ROLE_KEY)

    if not (user_id and org_id and email and role):
        # Not logged in → redirect to login with return URL
        return_url = (
            f"/oauth/authorize?client_id={url_encode(client_id)}"
            f"&redirect_uri={url_encode(redirect_uri)}"
            f"&response_type=code"
        )
        if code_challenge:
            return_url += f"&code_challenge={url_encode(code_challenge)}"
        if state:
            return_url += f"&state={url_encode(state)}"
        if scope:
            return_url += f"&scope={url_encode(scope)}"
        return RedirectResponse(f"/auth/login?return={url_encode(return_url)}")

    # Verify client exists and redirect_uri is registered
    pool = await get_pool()
    async with pool.acquire() as conn:
        oauth_client = await db_get_client(conn, client_id)
        if oauth_client is None:
            return Response("Unknown client_id", status_code=400)

        if redirect_uri not in (oauth_client["redirect_uris"] or []):
            return Response("redirect_uri not registered", status_code=400)

    if response_type != "code":
        return Response("Only response_type=code is supported", status_code=400)

    # Show consent screen with role-appropriate permission description
    client_name = oauth_client["client_name"] or "MCP Client"
    if role == "staff":
        role_label = "Staff"
        scopes_html = (
            "<ul>\n"
            "    <li>View and update your profile, contact info, and timezone</li>\n"
            "    <li>Manage your availability and certifications</li>\n"
            "    <li>View your assigned shifts and accept/decline assignments</li>\n"
            "  </ul>"
        )
    elif role == "client":
        role_label = "Client"
        scopes_html = (
            "<ul>\n"
            "    <li>View and update your profile and contact info</li>\n"
            "    <li>Submit, track, and cancel staffing requests</li>\n"
            "    <li>View available work sites and certifications</li>\n"
            "  </ul>"
        )
    else:
        role_label = "Admin"
        scopes_html = (
            "<ul>\n"
            "    <li>View and manage staff, shifts, and work sites</li>\n"
            "    <li>View and create clients and staffing requests</li>\n"
            "    <li>Run read-only SQL queries on your org's data</li>\n"
            "    <li>Generate magic login links for staff and clients</li>\n"
            "  </ul>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize {client_name} — Senzii</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8fafc; color: #1e293b; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .card {{ background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.1), 0 1px 2px rgba(0,0,0,0.06); padding: 2rem; max-width: 440px; width: 100%; }}
  h1 {{ font-size: 1.25rem; font-weight: 700; margin-bottom: 0.5rem; }}
  p {{ color: #64748b; font-size: 0.95rem; line-height: 1.5; margin-bottom: 1.5rem; }}
  .app-name {{ font-weight: 600; color: #16a34a; }}
  .org-name {{ font-weight: 500; }}
  .scopes {{ background: #f1f5f9; border-radius: 8px; padding: 1rem; margin-bottom: 1.5rem; }}
  .scopes li {{ list-style: none; font-size: 0.9rem; color: #475569; margin: 0.25rem 0; }}
  .scopes li::before {{ content: "✓ "; color: #16a34a; }}
  .actions {{ display: flex; gap: 0.75rem; }}
  .btn {{ flex: 1; padding: 0.75rem 1rem; border-radius: 8px; border: none; font-size: 0.95rem; font-weight: 600; cursor: pointer; text-decoration: none; text-align: center; }}
  .btn-allow {{ background: #16a34a; color: white; }}
  .btn-allow:hover {{ background: #15803d; }}
  .btn-deny {{ background: #f1f5f9; color: #64748b; }}
  .btn-deny:hover {{ background: #e2e8f0; }}
  .user-info {{ font-size: 0.85rem; color: #94a3b8; margin-top: 1.5rem; text-align: center; }}
  .role-badge {{ display: inline-block; background: #f1f5f9; color: #475569; font-size: 0.75rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 4px; margin-left: 0.5rem; }}
</style>
</head>
<body>
<div class="card">
  <h1>Authorize <span class="app-name">{client_name}</span><span class="role-badge">{role_label}</span></h1>
  <p><strong>{client_name}</strong> wants to connect to your Senzii organization <span class="org-name">({email})</strong>. It will be able to:</p>
  <div class="scopes">
    {scopes_html}
  </div>
  <form method="POST" action="/oauth/authorize">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="code_challenge" value="{code_challenge or ''}">
    <input type="hidden" name="state" value="{state or ''}">
    <input type="hidden" name="scope" value="{scope or ''}">
    <div class="actions">
      <button type="submit" name="action" value="deny" class="btn btn-deny">Deny</button>
      <button type="submit" name="action" value="allow" class="btn btn-allow">Allow</button>
    </div>
  </form>
  <div class="user-info">Signed in as {email}</div>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@router.post("/oauth/authorize")
async def authorize_post(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str | None = Form(None),
    state: str | None = Form(None),
    scope: str | None = Form(None),
    action: str = Form(...),
):
    """POST /oauth/authorize — handle consent form submission."""
    if action != "allow":
        # User denied — redirect back with error
        url = f"{redirect_uri}?error=access_denied"
        if state:
            url += f"&state={url_encode(state)}"
        return RedirectResponse(url)

    # Get user info from session
    session = request.session
    user_id = session.get(USER_ID_KEY)
    org_id = session.get(ORG_ID_KEY)
    role = session.get(ROLE_KEY)

    if not (user_id and org_id and role):
        return Response("Not logged in", status_code=401)

    # Resolve entity_id: staff_id for staff role, client_id for client role,
    # None for admin/super (they get org-wide access).
    if role == "staff":
        entity_id = session.get(STAFF_ID_KEY)
    elif role == "client":
        entity_id = session.get(CLIENT_ID_KEY)
    else:
        entity_id = None  # admin, super

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Generate authorization code
        code = f"ac_{uuid.uuid4().hex}"
        try:
            await db_create_authorization_code(
                conn, code, client_id, user_id, org_id,
                role, entity_id, redirect_uri, code_challenge, scope,
            )
        except Exception as e:
            return Response(f"Failed to create code: {e}", status_code=500)

    # Redirect back to client with code
    url = f"{redirect_uri}?code={url_encode(code)}"
    if state:
        url += f"&state={url_encode(state)}"
    return RedirectResponse(url)


# ── Token endpoint ───────────────────────────────────────────────────────────


async def _token_from_authorization_code(pool, req: dict) -> Response:
    """Exchange an authorization code for an access token + refresh token."""
    code = req.get("code")
    if not code:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Missing code"},
            status_code=400,
        )

    async with pool.acquire() as conn:
        code_data = await db_consume_authorization_code(conn, code)
        if code_data is None:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired code"},
                status_code=400,
            )

        # Verify client_id matches
        cid = req.get("client_id")
        if cid and cid != code_data["client_id"]:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        # Verify PKCE if challenge was set
        challenge = code_data["code_challenge"]
        if challenge:
            verifier = req.get("code_verifier")
            if not verifier:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "Missing code_verifier"},
                    status_code=400,
                )
            if not verify_pkce(verifier, challenge):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                    status_code=400,
                )

        # Create JWT access token (24-hour lifetime)
        secret = os.getenv("SESSION_SECRET", "senzii-oauth-secret")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=24)
        claims = {
            "sub": code_data["user_id"],
            "org_id": code_data["organization_id"],
            "role": code_data["role"],
            "entity_id": code_data["entity_id"],
            "client_id": code_data["client_id"],
            "scope": code_data["scope"],
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": "https://senzii.com",
        }
        access_token = create_jwt(claims, secret)

        # Store token hash for revocation tracking
        try:
            await db_store_token(
                conn, access_token, code_data["user_id"],
                code_data["organization_id"], code_data["client_id"], expires_at,
            )
        except Exception as e:
            print(f"[oauth] Failed to store token: {e}")

        # Issue a refresh token (1-year lifetime, rotates on each use)
        try:
            refresh_token = await db_create_refresh_token(
                conn,
                code_data["user_id"],
                code_data["organization_id"],
                code_data["client_id"],
                code_data["role"],
                code_data["entity_id"],
                code_data["scope"],
            )
        except Exception as e:
            print(f"[oauth] Failed to create refresh token: {e}")
            refresh_token = ""

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 86400,
            "refresh_token": refresh_token,
            "scope": code_data["scope"],
        }
    )


async def _token_from_refresh_token(pool, req: dict) -> Response:
    """Exchange a refresh token for a new access token + new refresh token.
    The old refresh token is consumed (single-use rotation).
    """
    refresh_token = req.get("refresh_token")
    if not refresh_token:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Missing refresh_token"},
            status_code=400,
        )

    async with pool.acquire() as conn:
        # Consume the old refresh token (single-use, deleted from DB)
        rt_data = await db_consume_refresh_token(conn, refresh_token)
        if rt_data is None:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired refresh token"},
                status_code=400,
            )

        # Mint new access token (24-hour lifetime)
        secret = os.getenv("SESSION_SECRET", "senzii-oauth-secret")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=24)
        claims = {
            "sub": rt_data["user_id"],
            "org_id": rt_data["organization_id"],
            "role": rt_data["role"],
            "entity_id": rt_data["entity_id"],
            "client_id": rt_data["client_id"],
            "scope": rt_data["scope"],
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": "https://senzii.com",
        }
        access_token = create_jwt(claims, secret)

        # Store new access token hash
        try:
            await db_store_token(
                conn, access_token, rt_data["user_id"],
                rt_data["organization_id"], rt_data["client_id"], expires_at,
            )
        except Exception as e:
            print(f"[oauth] Failed to store token: {e}")

        # Issue a new refresh token (rotation)
        try:
            new_refresh_token = await db_create_refresh_token(
                conn,
                rt_data["user_id"],
                rt_data["organization_id"],
                rt_data["client_id"],
                rt_data["role"],
                rt_data["entity_id"],
                rt_data["scope"],
            )
        except Exception as e:
            print(f"[oauth] Failed to create refresh token: {e}")
            new_refresh_token = ""

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 86400,
            "refresh_token": new_refresh_token,
            "scope": rt_data["scope"],
        }
    )


@router.post("/oauth/token")
async def token(request: Request):
    """POST /oauth/token — exchange authorization code for access token,
    or refresh an expired access token using a refresh token."""
    # Accept both form-encoded and JSON bodies (OAuth clients typically send form)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        req = await request.json()
    else:
        form = await request.form()
        req = {k: v for k, v in form.items()}

    grant_type = req.get("grant_type", "")
    pool = await get_pool()

    if grant_type == "authorization_code":
        return await _token_from_authorization_code(pool, req)
    elif grant_type == "refresh_token":
        return await _token_from_refresh_token(pool, req)
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ── GET /oauth/me — current user info from session ───────────────────────────


@router.get("/oauth/me")
async def me(request: Request):
    """GET /oauth/me — returns the current logged-in user's info from the session.

    Useful for MCP clients to display the connected user after OAuth.
    """
    session = request.session
    user_id = session.get(USER_ID_KEY)
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    return JSONResponse(
        {
            "user_id": user_id,
            "email": session.get(USER_EMAIL_KEY),
            "role": session.get(ROLE_KEY),
            "organization_id": session.get(ORG_ID_KEY),
            "staff_id": session.get(STAFF_ID_KEY),
            "client_id": session.get(CLIENT_ID_KEY),
        }
    )