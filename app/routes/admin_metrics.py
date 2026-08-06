"""Admin metrics route — dashboard org-level metric counts.
Direct port of src/routes/admin_metrics.rs (which calls db/metrics.rs::get_org_metrics).

All routes require admin auth (require_auth). org_id comes from the session.
Mounted under /api/admin prefix in main.py.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.middleware.auth import require_auth
from app.db.pool import get_pool

router = APIRouter()


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_metrics(request: Request):
    """GET /metrics — returns org metrics.

    Metrics: open_shifts, pending_requests, unassigned_staff, recent_confirmed.
    Mirrors Rust admin_metrics.rs::get_metrics → db/metrics.rs::get_org_metrics.

    - open_shifts: shifts where confirmed assignments < min_staff, start_time >= today.
    - pending_requests: staffing_requests with status = 'open'.
    - unassigned_staff: staff with no confirmed assignments ever.
    - recent_confirmed: assignments confirmed in the last 7 days.
    """
    try:
        _user_id, _role, _email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """WITH
                today AS (
                  SELECT CURRENT_DATE AS d
                ),
                open_shifts AS (
                  SELECT COUNT(*) as c
                  FROM shifts sh
                  JOIN work_sites ws ON ws.id = sh.site_id
                  WHERE ws.organization_id = $1
                    AND sh.start_time >= (SELECT d FROM today)
                    AND COALESCE(
                      (SELECT COUNT(*) FROM assignments a
                       WHERE a.shift_id = sh.id AND a.status = 'confirmed'),
                      0
                    ) < COALESCE(sh.min_staff, 1)
                ),
                pending_requests AS (
                  SELECT COUNT(*) as c
                  FROM staffing_requests sr
                  WHERE sr.organization_id = $1 AND sr.status = 'open'
                ),
                unassigned_staff AS (
                  SELECT COUNT(*) as c
                  FROM staff s
                  WHERE s.organization_id = $1
                    AND s.deleted_at IS NULL
                    AND NOT EXISTS (
                      SELECT 1 FROM assignments a
                      JOIN shifts sh ON sh.id = a.shift_id
                      JOIN work_sites ws ON ws.id = sh.site_id
                      WHERE a.staff_id = s.id
                        AND ws.organization_id = $1
                        AND a.status = 'confirmed'
                    )
                ),
                recent_confirmed AS (
                  SELECT COUNT(*) as c
                  FROM assignments a
                  JOIN shifts sh ON sh.id = a.shift_id
                  JOIN work_sites ws ON ws.id = sh.site_id
                  WHERE ws.organization_id = $1
                    AND a.status = 'confirmed'
                    AND a.confirmed_at >= NOW() - INTERVAL '7 days'
                )
                SELECT
                  (SELECT c FROM open_shifts)        AS open_shifts,
                  (SELECT c FROM pending_requests)   AS pending_requests,
                  (SELECT c FROM unassigned_staff)    AS unassigned_staff,
                  (SELECT c FROM recent_confirmed)    AS recent_confirmed""",
                org_id,
            )
        except Exception as e:
            print(f"Get org metrics error: {e}")
            return JSONResponse({"error": "Failed to fetch metrics"}, status_code=500)

    if row is None:
        return {
            "open_shifts": 0,
            "pending_requests": 0,
            "unassigned_staff": 0,
            "recent_confirmed": 0,
        }

    return {
        "open_shifts": row["open_shifts"],
        "pending_requests": row["pending_requests"],
        "unassigned_staff": row["unassigned_staff"],
        "recent_confirmed": row["recent_confirmed"],
    }