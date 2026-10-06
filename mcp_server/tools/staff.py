"""Staff MCP tools — list, get, create, archive staff; certifications; availability; magic links."""
import json
import uuid
from datetime import datetime

import asyncpg
from mcp.types import CallToolResult

from mcp_server.config import BASE_URL
from mcp_server.tools.common import _audit_log, _error_result, _json_result


async def list_staff(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
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


async def get_staff(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
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


async def create_staff(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
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


async def add_staff_certification(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    staff_id = arguments["staff_id"]
    cert_input = arguments["certification"]
    # The staff member must belong to the caller's org — otherwise a caller
    # could attach certifications to another organization's staff member
    # (cross-tenant write).
    owner = await conn.fetchval(
        "SELECT organization_id FROM staff WHERE id = $1 AND deleted_at IS NULL",
        staff_id,
    )
    if owner != org_id:
        return _error_result("Staff member not found")
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


async def set_staff_availability(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
    staff_id = arguments["staff_id"]
    # The staff member must belong to the caller's org — otherwise a caller
    # could overwrite another organization's availability windows
    # (cross-tenant write via DELETE + INSERT keyed only by staff_id).
    owner = await conn.fetchval(
        "SELECT organization_id FROM staff WHERE id = $1 AND deleted_at IS NULL",
        staff_id,
    )
    if owner != org_id:
        return _error_result("Staff member not found")
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


async def delete_staff(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
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


async def send_staff_magic_link(conn: asyncpg.Connection, org_id: int, user_id: int | None, role: str, arguments: dict) -> CallToolResult:
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