"""Organization certifications CRUD route.

Direct port of src/routes/org_certs.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Routes (mounted at /api/org-certs):
  GET    /         — list all certifications for the org
  POST   /         — create a new certification for the org
  PUT    /{id}     — rename a certification
  DELETE /{id}     — delete a certification from the org
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool
from app.db import audit_log

router = APIRouter()


# ── Request payloads ──────────────────────────────────────────────────────────


class CreateCertPayload(BaseModel):
    name: str


class UpdateCertPayload(BaseModel):
    name: str


# ── Handlers ──────────────────────────────────────────────────────────────────


@router.get("/")
async def list_certs(request: Request):
    """GET / — list all certifications for the org."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, created_at FROM organization_certifications "
            "WHERE organization_id = $1 ORDER BY name",
            org_id,
        )
        certs = [
            {
                "id": r["id"],
                "name": r["name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

    return JSONResponse({"certifications": certs})


@router.post("/")
async def create_cert(payload: CreateCertPayload, request: Request):
    """POST / — create a new certification for the org."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    name = payload.name.strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO organization_certifications (organization_id, name) "
                "VALUES ($1, $2) RETURNING id, name, created_at",
                org_id, name,
            )
        except Exception as e:
            msg = str(e)
            # Unique violation — cert name already exists for this org
            if "23505" in msg or "duplicate key" in msg or "org_certs_org_name_uniq" in msg:
                return JSONResponse(
                    {"error": "Certification already exists"}, status_code=409
                )
            print(f"[org_certs] Create org cert error: {e}")
            return JSONResponse(
                {"error": "Internal server error"}, status_code=500
            )

        cert = {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        await audit_log.log(
            conn, org_id, user_id,
            "org_cert.create", "organization_certification",
            cert["id"], None, cert, None,
        )

    return JSONResponse(cert, status_code=201)


@router.put("/{id}")
async def update_cert(id: int, payload: UpdateCertPayload, request: Request):
    """PUT /{id} — rename a certification."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    name = payload.name.strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "UPDATE organization_certifications SET name = $3 "
                "WHERE id = $1 AND organization_id = $2 "
                "RETURNING id, name, created_at",
                id, org_id, name,
            )
        except Exception as e:
            msg = str(e)
            if "23505" in msg or "duplicate key" in msg or "org_certs_org_name_uniq" in msg:
                return JSONResponse(
                    {"error": "Certification name already exists"}, status_code=409
                )
            print(f"[org_certs] Update org cert error: {e}")
            return JSONResponse(
                {"error": "Internal server error"}, status_code=500
            )

        if row is None:
            return JSONResponse(
                {"error": "Certification not found"}, status_code=404
            )

        cert = {
            "id": row[0],
            "name": row[1],
            "created_at": row[2].isoformat() if row[2] else None,
        }

    return JSONResponse(cert)


@router.delete("/{id}")
async def delete_cert(id: int, request: Request):
    """DELETE /{id} — delete a certification from the org."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "DELETE FROM organization_certifications "
                "WHERE id = $1 AND organization_id = $2 RETURNING id",
                id, org_id,
            )
        except Exception as e:
            print(f"[org_certs] Delete org cert error: {e}")
            return JSONResponse(
                {"error": "Internal server error"}, status_code=500
            )

        if row is None:
            return JSONResponse(
                {"error": "Certification not found"}, status_code=404
            )

        before = {"id": id}
        await audit_log.log(
            conn, org_id, user_id,
            "org_cert.delete", "organization_certification",
            id, before, None, None,
        )

    return JSONResponse({"ok": True})


# ── Public helper (called by staff/client portal routers) ────────────────────


async def list_certs_for_org(pool, org_id: int) -> list[dict]:
    """List org certifications given an org_id.

    Used by staff and client portal handlers so they don't need to
    duplicate the query. Mirrors the Rust public helper.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM organization_certifications "
            "WHERE organization_id = $1 ORDER BY name",
            org_id,
        )
    return [{"id": r["id"], "name": r["name"]} for r in rows]