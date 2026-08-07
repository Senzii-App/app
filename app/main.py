"""Senzii Web API — FastAPI application.

Serves the Senzii staffing platform: authentication, shift scheduling and
staff matching, work sites, assignments, checkout, admin dashboards, staff
and client portals, billing/seats, and public tracking/lead endpoints.
"""
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import PORT, SESSION_SECRET
from app.db.migrations import run_migrations
from app.db.pool import close_pool, create_pool, get_pool
from app.db.session_store import delete_expired
from app.routes import (
    admin, admin_clients, admin_metrics, admin_requests, admin_staff,
    admin_team, api_keys, assignments, audit_log, auth, checkout,
    client_portal, leads, oauth, org_certs, pages, sales_activities,
    sales_inquiries, seats, shifts, staff, staff_portal, track, webhook,
    work_sites,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    # Startup
    pool = await create_pool()
    async with pool.acquire() as conn:
        await run_migrations(conn)

    # Start session cleanup background task
    async def cleanup_expired_sessions():
        while True:
            await asyncio.sleep(3600)  # 1 hour
            try:
                p = await get_pool()
                async with p.acquire() as conn:
                    n = await delete_expired(conn)
                    if n > 0:
                        print(f"Cleaned up {n} expired sessions")
            except Exception as e:  # noqa: BLE001 - keep the cleanup loop alive on transient errors
                print(f"Failed to clean up expired sessions: {e}")

    cleanup_task = asyncio.create_task(cleanup_expired_sessions())

    yield

    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await close_pool()


app = FastAPI(lifespan=lifespan)

# ── Middleware ───────────────────────────────────────────────────────────────
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="session",
    max_age=86400 * 7,  # 7 days
    https_only=False,  # Caddy handles TLS
    same_site="lax",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ── Health check ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "healthy"}

# ── Webhook (needs raw body) ────────────────────────────────────────────────
app.router.routes.extend(webhook.router.routes)

# ── Auth ─────────────────────────────────────────────────────────────────────
app.include_router(auth.router, prefix="/auth")

# ── OAuth 2.1 ──────────────────────────────────────────────────────────────
app.include_router(oauth.router)

# ── Pages ───────────────────────────────────────────────────────────────────
# All page routes are registered in pages.router with their full paths
app.include_router(pages.router)

# ── Admin pages ────────────────────────────────────────────────────────────
app.include_router(admin.router)

# ── Checkout ────────────────────────────────────────────────────────────────
# checkout.router has prefix='' and includes the /api/checkout path itself.
app.include_router(checkout.router)

# ── Public API routes ──────────────────────────────────────────────────────
app.include_router(track.router, prefix="/api")
app.include_router(leads.router, prefix="/api")
app.include_router(sales_inquiries.router, prefix="/api")

# ── Protected API routes ──────────────────────────────────────────────────
app.include_router(shifts.router, prefix="/api/shifts")
app.include_router(staff.router, prefix="/api/staff")
app.include_router(work_sites.router, prefix="/api/work-sites")
app.include_router(assignments.router, prefix="/api/assignments")

# ── Admin API routes ──────────────────────────────────────────────────────
app.include_router(admin_staff.router, prefix="/api/admin")
app.include_router(admin_clients.router, prefix="/api/admin")
app.include_router(admin_requests.router, prefix="/api/admin")
app.include_router(admin_metrics.router, prefix="/api/admin")
app.include_router(admin_team.router, prefix="/api/admin")
app.include_router(track.admin_router, prefix="/api/admin")
app.include_router(leads.admin_router, prefix="/api/admin")
app.include_router(sales_inquiries.admin_router, prefix="/api/admin")
app.include_router(sales_activities.admin_router, prefix="/api/admin")
app.include_router(audit_log.router, prefix="/api/admin")

# ── Settings / API keys ──────────────────────────────────────────────────
app.include_router(api_keys.router, prefix="/api/settings")

# ── Seats (billing) ──────────────────────────────────────────────────────
app.include_router(seats.router, prefix="/api/seats")

# ── Org certifications ───────────────────────────────────────────────────
app.include_router(org_certs.router, prefix="/api/org-certs")

# ── Portals ──────────────────────────────────────────────────────────────
app.include_router(staff_portal.router, prefix="/staff/api")
app.include_router(client_portal.router, prefix="/client/api")

# ── Static files (fallback) ──────────────────────────────────────────────────
# check_dir=False: don't crash startup if css/js dirs are missing/empty
app.mount("/css", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "..", "site", "css"), check_dir=False), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "..", "site", "js"), check_dir=False), name="js")
# Fallback for any other static file
@app.get("/{path:path}")
async def static_fallback(path: str):
    """Serve static files as fallback. Matches the Rust ServeDir fallback."""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "site")
    file_path = os.path.join(static_dir, path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return JSONResponse({"error": "Not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)