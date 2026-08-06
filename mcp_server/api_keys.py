"""API key verification — port of mcp_server/src/db/api_keys.rs."""
import hashlib
import hmac

import asyncpg


async def verify_api_key(conn: asyncpg.Connection, key: str) -> tuple[int, int] | None:
    """Verify an API key. Returns (user_id, org_id) if valid, None otherwise."""
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    row = await conn.fetchrow(
        """SELECT user_id, organization_id FROM api_keys
           WHERE key_hash = $1 AND revoked_at IS NULL""",
        key_hash,
    )
    if row is None:
        return None
    # Update last_used_at
    await conn.execute(
        "UPDATE api_keys SET last_used_at = NOW() WHERE key_hash = $1",
        key_hash,
    )
    return (row["user_id"], row["organization_id"])