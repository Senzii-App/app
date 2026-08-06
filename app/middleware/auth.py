"""Auth middleware — session extraction and auth checks.
Direct port of src/middleware/auth.rs and the auth helpers in src/routes/auth.rs.

Uses the same session keys as the Rust app:
- userId, userEmail, role, organizationId, staffId, clientId
- sessionStartAt, lastActivityAt
"""
import time
from typing import Any

from fastapi import Request, HTTPException
from app.config import SESSION_IDLE_TIMEOUT_SECS, SESSION_ABSOLUTE_TIMEOUT_SECS


# Session keys (must match Rust exactly)
USER_ID_KEY = "userId"
USER_EMAIL_KEY = "userEmail"
ROLE_KEY = "role"
ORG_ID_KEY = "organizationId"
STAFF_ID_KEY = "staffId"
CLIENT_ID_KEY = "clientId"
SESSION_START_KEY = "sessionStartAt"
LAST_ACTIVITY_KEY = "lastActivityAt"


def _get_session(request: Request) -> dict[str, Any]:
    """Get the session dict from the request (starlette SessionMiddleware)."""
    return request.session


def check_session_timeouts(request: Request):
    """Check idle and absolute session timeouts. Raises 401 if exceeded."""
    session = _get_session(request)
    now = int(time.time())
    last_activity = session.get(LAST_ACTIVITY_KEY)
    session_start = session.get(SESSION_START_KEY)

    if last_activity is None or session_start is None:
        session[SESSION_START_KEY] = now
        session[LAST_ACTIVITY_KEY] = now
        return

    if now - last_activity > SESSION_IDLE_TIMEOUT_SECS:
        session.clear()
        raise HTTPException(status_code=401, detail="Session expired")

    if now - session_start > SESSION_ABSOLUTE_TIMEOUT_SECS:
        session.clear()
        raise HTTPException(status_code=401, detail="Session expired")

    session[LAST_ACTIVITY_KEY] = now


def require_auth(request: Request) -> tuple[int, str, str, int]:
    """Require admin or super session. Returns (user_id, role, email, org_id)."""
    check_session_timeouts(request)
    session = _get_session(request)
    user_id = session.get(USER_ID_KEY)
    role = session.get(ROLE_KEY)
    org_id = session.get(ORG_ID_KEY)
    email = session.get(USER_EMAIL_KEY)

    if user_id and role in ("admin", "super") and org_id is not None and email:
        return (user_id, role, email, org_id)
    raise HTTPException(status_code=401, detail="Not authenticated")


def require_staff_session(request: Request) -> tuple[int, int, int]:
    """Require staff session. Returns (user_id, org_id, staff_id)."""
    check_session_timeouts(request)
    session = _get_session(request)
    user_id = session.get(USER_ID_KEY)
    role = session.get(ROLE_KEY)
    org_id = session.get(ORG_ID_KEY)
    staff_id = session.get(STAFF_ID_KEY)

    if user_id and role == "staff" and org_id is not None and staff_id:
        return (user_id, org_id, staff_id)
    raise HTTPException(status_code=401, detail="Not authenticated")


def require_client_session(request: Request) -> tuple[int, int, int]:
    """Require client session. Returns (user_id, org_id, client_id)."""
    check_session_timeouts(request)
    session = _get_session(request)
    user_id = session.get(USER_ID_KEY)
    role = session.get(ROLE_KEY)
    org_id = session.get(ORG_ID_KEY)
    client_id = session.get(CLIENT_ID_KEY)

    if user_id and role == "client" and org_id is not None and client_id:
        return (user_id, org_id, client_id)
    raise HTTPException(status_code=401, detail="Not authenticated")


def require_super_session(request: Request) -> tuple[int, str]:
    """Require super-user session. Returns (user_id, email)."""
    check_session_timeouts(request)
    session = _get_session(request)
    user_id = session.get(USER_ID_KEY)
    role = session.get(ROLE_KEY)
    email = session.get(USER_EMAIL_KEY)

    if user_id and role == "super" and email:
        return (user_id, email)
    raise HTTPException(status_code=401, detail="Super-user access required")


def save_session(request: Request, user_id: int, email: str, role: str, org_id: int):
    """Save session data and stamp timeout timestamps."""
    now = int(time.time())
    session = _get_session(request)
    session[USER_ID_KEY] = user_id
    session[USER_EMAIL_KEY] = email
    session[ROLE_KEY] = role
    session[ORG_ID_KEY] = org_id
    session[SESSION_START_KEY] = now
    session[LAST_ACTIVITY_KEY] = now


def destroy_session(request: Request):
    """Destroy session data."""
    session = _get_session(request)
    session.clear()