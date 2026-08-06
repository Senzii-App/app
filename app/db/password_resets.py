"""Password reset tokens — direct port of src/db/password_resets.rs."""
import uuid
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

import asyncpg

RESET_EXPIRY_HOURS = 1


class VerifiedResetToken(NamedTuple):
    user_id: int
    user_email: str


async def create_password_reset_token(conn: asyncpg.Connection, user_id: int) -> str:
    """Create a password reset token, invalidate old ones, return the new token."""
    token = str(uuid.uuid4())
    # Invalidate any existing tokens for this user
    await conn.execute(
        "UPDATE password_resets SET used_at = NOW() WHERE user_id = $1 AND used_at IS NULL",
        user_id,
    )
    await conn.execute(
        "INSERT INTO password_resets (user_id, token, expires_at) VALUES ($1, $2, NOW() + INTERVAL '1 hour')",
        user_id,
        token,
    )
    return token


async def verify_password_reset_token(conn: asyncpg.Connection, token: str) -> VerifiedResetToken | None:
    """Verify a reset token. Returns (user_id, user_email) if valid, None otherwise."""
    row = await conn.fetchrow(
        """SELECT pr.user_id, u.email
           FROM password_resets pr
           JOIN users u ON u.id = pr.user_id
           WHERE pr.token = $1 AND pr.expires_at > NOW() AND pr.used_at IS NULL""",
        token,
    )
    if row is None:
        return None
    return VerifiedResetToken(user_id=row["user_id"], user_email=row["email"])


async def consume_password_reset_token(conn: asyncpg.Connection, token: str):
    """Mark a reset token as used."""
    await conn.execute(
        "UPDATE password_resets SET used_at = NOW() WHERE token = $1",
        token,
    )