"""Work site CRUD endpoints (org-scoped) — FastAPI port of src/routes/work_sites.rs.

All routes require admin auth. org_id comes exclusively from the session.
All SQL queries are identical to the Rust implementation.
"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Any

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db.audit_log import log as audit_log
from app.services.geocode import geocode_address

router = APIRouter()


# ── Request payloads ───────────────────────────────────────────────────────────

class CreateWorkSitePayload(BaseModel):
    name: str
    address: str | None = None
    required_skills: Any | None = None  # JSON value (array of strings)
    timezone: str | None = None


class UpdateWorkSitePayload(BaseModel):
    name: str | None = None
    # Rust uses Option<Option<String>> to distinguish "not provided" from
    # "clear to null". JSON/pydantic collapses this, so None = keep current,
    # "" = clear, any other string = set. See handler for the full logic.
    address: str | None = None
    required_skills: Any | None = None
    timezone: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape single quotes for simple_query SQL (matches Rust's esc)."""
    return s.replace("'", "''")


def _parse_json_col(val):
    """Parse a JSON/JSONB column value from asyncpg (str or None) to Python."""
    if val is None:
        return None
    if isinstance(val, str):
        return json.loads(val)
    return val  # already decoded


def _site_dict_from_row(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "address": row[2],
        "latitude": float(row[3]),
        "longitude": float(row[4]),
        "required_skills": _parse_json_col(row[5]),
        "timezone": row[6],
        "organization_id": row[7],
    }


# ── Handlers ───────────────────────────────────────────────────────────────────

# GET / — list all work sites in the org
@router.get("/")
async def list_work_sites(request: Request):
    user_id, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ws.id, ws.name, ws.address, COALESCE(ws.latitude, 0)::double precision AS latitude,
             COALESCE(ws.longitude, 0)::double precision AS longitude, ws.required_skills, ws.timezone,
             ws.organization_id, COALESCE(sc.cnt, 0) AS shift_count, COALESCE(rc.cnt, 0) AS request_count
             FROM work_sites ws
             LEFT JOIN LATERAL (SELECT COUNT(*) AS cnt FROM shifts WHERE site_id = ws.id AND organization_id = $1) sc ON true
             LEFT JOIN LATERAL (SELECT COUNT(*) AS cnt FROM staffing_requests WHERE site_id = ws.id AND organization_id = $1) rc ON true
             WHERE ws.organization_id = $1 ORDER BY ws.name""",
            org_id,
        )
        sites = [
            {
                "id": r[0],
                "name": r[1],
                "address": r[2],
                "latitude": r[3],
                "longitude": r[4],
                "required_skills": _parse_json_col(r[5]),
                "timezone": r[6],
                "organization_id": r[7],
                "shift_count": r[8],
                "request_count": r[9],
            }
            for r in rows
        ]
        return {"work_sites": sites}


# GET /{id} — get a single work site
@router.get("/{id}")
async def get_work_site(request: Request, id: int):
    user_id, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, name, address, COALESCE(latitude, 0)::double precision AS latitude, COALESCE(longitude, 0)::double precision AS longitude, required_skills, timezone, organization_id FROM work_sites WHERE id = $1 AND organization_id = $2""",
            id, org_id,
        )
        if row is None:
            return JSONResponse({"error": "Work site not found"}, status_code=404)
        return _site_dict_from_row(row)


# POST / — create a work site
@router.post("/")
async def create_work_site(request: Request, payload: CreateWorkSitePayload):
    user_id, _, _, org_id = require_auth(request)

    if not payload.name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    # Resolve coordinates by geocoding from address
    latitude, longitude = None, None
    if payload.address and payload.address.strip():
        coords = await geocode_address(payload.address)
        if coords:
            latitude, longitude = coords.latitude, coords.longitude
        else:
            print("[work_sites] Could not geocode address, storing without coordinates")

    pool = await get_pool()
    async with pool.acquire() as conn:
        required_skills = payload.required_skills if payload.required_skills is not None else []
        timezone = payload.timezone or "UTC"

        # Neon pooler doesn't reliably return rows from simple_query RETURNING.
        # Use simple_query for INSERT (no RETURNING), then fetch by unique name+org.
        addr_sql = f"'{_esc(payload.address)}'" if payload.address else "NULL"
        lat_sql = str(latitude) if latitude is not None else "NULL"
        lon_sql = str(longitude) if longitude is not None else "NULL"
        skills_str = json.dumps(required_skills) if required_skills is not None else "[]"
        insert_sql = (
            f"INSERT INTO work_sites (name, address, latitude, longitude, required_skills, timezone, organization_id) "
            f"VALUES ('{_esc(payload.name)}', {addr_sql}, {lat_sql}, {lon_sql}, "
            f"'{_esc(skills_str)}'::jsonb, '{_esc(timezone)}', {org_id})"
        )
        try:
            await conn.execute(insert_sql)
        except Exception as e:
            err_str = str(e)
            if "23505" in err_str or "duplicate key" in err_str:
                return JSONResponse({"error": "Work site with this name already exists"}, status_code=409)
            print(f"Create work site error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        # Fetch the newly created site by name + org (unique enough).
        # Neon read replica may lag behind the write — retry up to 5 times with 300ms delay.
        fetch_sql = (
            "SELECT id, name, address, COALESCE(latitude, 0)::double precision AS latitude, "
            "COALESCE(longitude, 0)::double precision AS longitude, required_skills, timezone, organization_id "
            "FROM work_sites WHERE name = $1 AND organization_id = $2 ORDER BY id DESC LIMIT 1"
        )
        row = None
        for attempt in range(5):
            try:
                row = await conn.fetchrow(fetch_sql, payload.name, org_id)
                if row is not None:
                    break
            except Exception as e:
                if attempt < 4:
                    print(f"Create work site fetch attempt {attempt + 1} failed (Neon replication lag): {e}")
                    await asyncio.sleep(0.3)
                else:
                    print(f"Create work site fetch error after retries: {e}")
                    return JSONResponse({"error": "Internal server error"}, status_code=500)
            if row is None and attempt < 4:
                await asyncio.sleep(0.3)

        if row is not None:
            site = _site_dict_from_row(row)
            site_id = site["id"]
            await audit_log(conn, org_id, user_id, "work_site.create", "work_site", site_id, None, site)
            return JSONResponse(site, status_code=201)

        return JSONResponse({"error": "Internal server error"}, status_code=500)


# POST /{id}/geocode — geocode a work site's address and update its coordinates
@router.post("/{id}/geocode")
async def geocode_work_site(request: Request, id: int):
    user_id, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Fetch the work site
        row = await conn.fetchrow(
            "SELECT id, name, address, COALESCE(latitude, 0)::double precision, COALESCE(longitude, 0)::double precision FROM work_sites WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if row is None:
            return JSONResponse({"error": "Work site not found"}, status_code=404)

        address = row["address"]
        if not address or not address.strip():
            return JSONResponse({"error": "Work site has no address to geocode"}, status_code=400)

        # Geocode
        coords = await geocode_address(address)
        if coords is None:
            return JSONResponse({"error": "Could not geocode address"}, status_code=422)

        # Update the work site
        try:
            await conn.execute(
                "UPDATE work_sites SET latitude = $1::float8, longitude = $2::float8, updated_at = NOW() WHERE id = $3 AND organization_id = $4",
                coords.latitude, coords.longitude, id, org_id,
            )
        except Exception as e:
            print(f"Geocode work site update error: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        # Fetch updated row
        updated = await conn.fetchrow(
            "SELECT id, name, address, COALESCE(latitude, 0)::double precision AS latitude, COALESCE(longitude, 0)::double precision AS longitude, required_skills, timezone, organization_id FROM work_sites WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if updated is None:
            return JSONResponse({"error": "Internal server error"}, status_code=500)

        return _site_dict_from_row(updated)


# DELETE /{id} — delete a work site that has no associated records.
# If shifts or staffing_requests reference this site, returns 409 Conflict.
@router.delete("/{id}")
async def delete_work_site(request: Request, id: int):
    user_id, _, _, org_id = require_auth(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify work site belongs to this org
        exists = await conn.fetchval(
            "SELECT id FROM work_sites WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if exists is None:
            return JSONResponse({"error": "Work site not found"}, status_code=404)

        # Check for shifts referencing this work site
        shift_count = await conn.fetchval(
            "SELECT COUNT(*) FROM shifts WHERE site_id = $1 AND organization_id = $2",
            id, org_id,
        )
        if shift_count and shift_count > 0:
            return JSONResponse({
                "error": f"Cannot delete work site — it has {shift_count} shift(s). Remove or reassign shifts first."
            }, status_code=409)

        # Check for staffing requests referencing this work site
        request_count = await conn.fetchval(
            "SELECT COUNT(*) FROM staffing_requests WHERE site_id = $1 AND organization_id = $2",
            id, org_id,
        )
        if request_count and request_count > 0:
            return JSONResponse({
                "error": f"Cannot delete work site — it has {request_count} staffing request(s). Remove or reassign requests first."
            }, status_code=409)

        # Safe to delete — shifts cascade-deleted by FK, staffing_requests set site_id to NULL by FK
        # but we've already confirmed there are none, so just delete the work site
        rows = await conn.execute(
            "DELETE FROM work_sites WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        # asyncpg returns "DELETE N" string; check actual count
        deleted_count = int(rows.split()[-1]) if isinstance(rows, str) else rows
        if deleted_count == 0:
            return JSONResponse({"error": "Work site not found"}, status_code=404)

        before = {"id": id}
        await audit_log(conn, org_id, user_id, "work_site.delete", "work_site", id, before, None)
        return {"deleted": True}


# PUT /{id} — update a work site
@router.put("/{id}")
async def update_work_site(request: Request, id: int, payload: UpdateWorkSitePayload):
    user_id, _, _, org_id = require_auth(request)

    if payload.name is not None and not payload.name:
        return JSONResponse({"error": "name cannot be empty"}, status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Verify work site belongs to this org and fetch current values
        current = await conn.fetchrow(
            "SELECT name, address, latitude, longitude, required_skills, timezone FROM work_sites WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if current is None:
            return JSONResponse({"error": "Work site not found"}, status_code=404)

        # Merge provided fields with current values.
        # Note: Rust uses Option<Option<String>> for address to distinguish
        # "not provided" from "clear to null". JSON/pydantic collapses this,
        # so we treat `None` as "keep current" (matching the common case).
        # To explicitly clear the address, a caller would send `address: ""`
        # which we treat as a cleared address below.
        current_name = current[0]
        current_address = current[1]
        current_skills = current[4] if current[4] is not None else []
        current_tz = current[5]

        final_name = payload.name if payload.name is not None else current_name
        # address handling: None → keep current; "" → clear; otherwise → set
        if payload.address is None:
            final_address = current_address
        elif payload.address == "":
            final_address = None
        else:
            final_address = payload.address

        final_skills = payload.required_skills if payload.required_skills is not None else current_skills
        final_tz = payload.timezone if payload.timezone is not None else current_tz

        # Re-geocode if address was provided (i.e., field was present in the payload)
        if payload.address is not None:
            if final_address:
                coords = await geocode_address(final_address)
                if coords:
                    final_lat = coords.latitude
                    final_lon = coords.longitude
                else:
                    final_lat = None
                    final_lon = None
            else:
                final_lat = None
                final_lon = None
        else:
            # Keep existing coordinates
            final_lat = current[2]
            final_lon = current[3]

        skills_str = json.dumps(final_skills) if final_skills is not None else "[]"
        lat_sql = str(final_lat) if final_lat is not None else "NULL"
        lon_sql = str(final_lon) if final_lon is not None else "NULL"
        addr_sql = f"'{_esc(final_address)}'" if final_address is not None else "NULL"

        update_sql = (
            f"UPDATE work_sites SET name = '{_esc(final_name)}', address = {addr_sql}, "
            f"latitude = {lat_sql}, longitude = {lon_sql}, "
            f"required_skills = '{_esc(skills_str)}'::jsonb, "
            f"timezone = '{_esc(final_tz)}', updated_at = NOW() "
            f"WHERE id = {id} AND organization_id = {org_id}"
        )

        try:
            await conn.execute(update_sql)
        except Exception as e:
            print(f"Update work site error: {e}")
            return JSONResponse({"error": "Failed to update work site"}, status_code=500)

        # Fetch updated row
        updated = await conn.fetchrow(
            "SELECT id, name, address, COALESCE(latitude, 0)::double precision AS latitude, COALESCE(longitude, 0)::double precision AS longitude, required_skills, timezone, organization_id FROM work_sites WHERE id = $1 AND organization_id = $2",
            id, org_id,
        )
        if updated is None:
            return JSONResponse({"error": "Database error"}, status_code=500)

        site = _site_dict_from_row(updated)
        await audit_log(conn, org_id, user_id, "work_site.update", "work_site", id, None, site)
        return site