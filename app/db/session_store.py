"""Session store — PostgreSQL-backed session storage.
Mirrors the Rust PostgresStore from tower-sessions.
Uses the same `sessions` table as the Rust app.
"""
import json
import time
import uuid
from typing import Any

import asyncpg


async def create_session(conn: asyncpg.Connection) -> str:
    """Create a new session and return its ID."""
    session_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO sessions (id, data, expiry_date) VALUES ($1, $2, $3)",
        session_id,
        json.dumps({}),
        time.time() + 86400 * 7,  # 7 days
    )
    return session_id


async def get_session_data(conn: asyncpg.Connection, session_id: str) -> dict[str, Any]:
    """Get session data by ID. Returns empty dict if not found or expired."""
    row = await conn.fetchrow(
        "SELECT data FROM sessions WHERE id = $1 AND expiry_date > NOW()",
        session_id,
    )
    if row is None:
        return {}
    return json.loads(row["data"])


async def set_session_data(conn: asyncpg.Connection, session_id: str, data: dict[str, Any]):
    """Update session data."""
    await conn.execute(
        "UPDATE sessions SET data = $1 WHERE id = $2",
        json.dumps(data),
        session_id,
    )


async def delete_session(conn: asyncpg.Connection, session_id: str):
    """Delete a session."""
    await conn.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def delete_expired(conn: asyncpg.Connection) -> int:
    """Delete all expired sessions. Returns count deleted."""
    result = await conn.execute(
        "DELETE FROM sessions WHERE expiry_date <= NOW()",
    )
    # asyncpg returns "DELETE N" — extract N
    parts = result.split()
    return int(parts[1]) if len(parts) > 1 else 0