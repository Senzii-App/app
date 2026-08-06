"""Audit log — append-only audit trail for HIPAA §164.312(b).
Direct port of src/db/audit_log.rs.
"""
import json
from typing import Any

import asyncpg


async def log(
    conn: asyncpg.Connection,
    org_id: int,
    user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    ip_address: str | None = None,
):
    """Insert an audit log entry. Fire-and-forget — errors are logged but not raised."""
    try:
        await conn.execute(
            """INSERT INTO audit_log (organization_id, user_id, action, entity_type, entity_id, before, after, ip_address)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            org_id,
            user_id,
            action,
            entity_type,
            entity_id,
            json.dumps(before) if before else None,
            json.dumps(after) if after else None,
            ip_address,
        )
    except Exception as e:
        # Audit log failures should not break the request
        print(f"[audit_log] Failed to log: {e}")