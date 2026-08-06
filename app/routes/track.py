"""Track route — first-party analytics endpoint.

Direct port of src/routes/track.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Routes:
  POST /track          — public analytics beacon (mounted at /api)
  GET  /page-views     — admin-only aggregate stats (mounted at /api/admin)
"""
import hashlib
import os
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.db.pool import get_pool

router = APIRouter()
admin_router = APIRouter()


# ── Request payload from the JS beacon ────────────────────────────────────────


class TrackPayload(BaseModel):
    page: str | None = None
    referrer: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    language: str | None = None
    section: str | None = None


# ── Visitor hashing ───────────────────────────────────────────────────────────


def visitor_hash_from_ip(ip: str) -> str:
    """Derive a daily-rotating, one-way visitor hash from IP + current date.

    Same person on different days = different hashes (no cross-day tracking).
    Same person on same day = same hash (deduplication for unique visitor counts).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    salt = os.getenv("VISITOR_HASH_SALT", "senzii-analytics-salt")
    inp = f"{ip}:{today}:{salt}"
    return hashlib.sha256(inp.encode()).hexdigest()


def extract_client_ip(request: Request) -> str:
    """Extract the real client IP from headers (Caddy sets X-Forwarded-For)."""
    # X-Forwarded-For: client, proxy1, proxy2 — take the first
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    # X-Real-Ip (set by some proxies)
    xri = request.headers.get("x-real-ip")
    if xri:
        trimmed = xri.strip()
        if trimmed:
            return trimmed
    # No proxy headers — use a fallback so we still record something
    return "unknown"


# ── POST /track ───────────────────────────────────────────────────────────────


@router.post("/track")
async def track_page_view(payload: TrackPayload, request: Request):
    """Records a page view event. No auth required — this is public.

    Uses 202 Accepted so bots don't get useful info.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        ip = extract_client_ip(request)
        visitor_hash = visitor_hash_from_ip(ip)

        page = payload.page or "/"
        try:
            await conn.execute(
                """INSERT INTO page_views
                   (visitor_hash, page, referrer, utm_source, utm_medium,
                    utm_campaign, utm_content, utm_term, screen_width,
                    screen_height, language, section)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
                visitor_hash, page,
                payload.referrer, payload.utm_source, payload.utm_medium,
                payload.utm_campaign, payload.utm_content, payload.utm_term,
                payload.screen_width, payload.screen_height,
                payload.language, payload.section,
            )
        except Exception as e:
            print(f"[track] Failed to insert page view: {e}")
            return Response(status_code=500)

    return Response(status_code=202)


# ── GET /page-views?days=30 ───────────────────────────────────────────────────


@admin_router.get("/page-views")
async def get_page_views(request: Request, days: int | None = None):
    """Returns aggregate page view stats. Requires super role."""
    # Only super users can see analytics
    role = request.session.get("role")
    if role != "super":
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    days = days if days is not None else 30
    days = max(1, min(days, 365))

    pool = await get_pool()
    async with pool.acquire() as conn:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        total_views = await conn.fetchval(
            "SELECT COUNT(*) as c FROM page_views WHERE created_at >= $1",
            cutoff,
        )
        unique_visitors = await conn.fetchval(
            "SELECT COUNT(DISTINCT visitor_hash) as c FROM page_views WHERE created_at >= $1",
            cutoff,
        )
        unique_days = await conn.fetchval(
            "SELECT COUNT(DISTINCT DATE(created_at)) as c FROM page_views WHERE created_at >= $1",
            cutoff,
        )

        top_referrers = await conn.fetch(
            "SELECT referrer, COUNT(*) as count FROM page_views "
            "WHERE created_at >= $1 GROUP BY referrer ORDER BY count DESC LIMIT 10",
            cutoff,
        )
        top_pages = await conn.fetch(
            "SELECT page, COUNT(*) as count FROM page_views "
            "WHERE created_at >= $1 GROUP BY page ORDER BY count DESC LIMIT 10",
            cutoff,
        )
        utm_summary = await conn.fetch(
            "SELECT utm_source, utm_medium, utm_campaign, COUNT(*) as count "
            "FROM page_views WHERE utm_source IS NOT NULL AND created_at >= $1 "
            "GROUP BY utm_source, utm_medium, utm_campaign "
            "ORDER BY count DESC LIMIT 20",
            cutoff,
        )
        section_views = await conn.fetch(
            "SELECT section, COUNT(*) as count FROM page_views "
            "WHERE section IS NOT NULL AND created_at >= $1 "
            "GROUP BY section ORDER BY count DESC LIMIT 20",
            cutoff,
        )
        recent_daily = await conn.fetch(
            "SELECT DATE(created_at)::text as date, COUNT(*) as views, "
            "COUNT(DISTINCT visitor_hash) as visitors FROM page_views "
            "WHERE created_at >= $1 GROUP BY DATE(created_at) ORDER BY date DESC",
            cutoff,
        )

        stats = {
            "total_views": total_views,
            "unique_visitors": unique_visitors,
            "unique_days": unique_days,
            "top_referrers": [
                {"referrer": r["referrer"], "count": r["count"]}
                for r in top_referrers
            ],
            "top_pages": [
                {"page": r["page"], "count": r["count"]} for r in top_pages
            ],
            "utm_summary": [
                {
                    "utm_source": r["utm_source"],
                    "utm_medium": r["utm_medium"],
                    "utm_campaign": r["utm_campaign"],
                    "count": r["count"],
                }
                for r in utm_summary
            ],
            "section_views": [
                {"section": r["section"], "count": r["count"]}
                for r in section_views
            ],
            "recent_daily": [
                {"date": r["date"], "views": r["views"], "visitors": r["visitors"]}
                for r in recent_daily
            ],
        }

    return JSONResponse(stats)