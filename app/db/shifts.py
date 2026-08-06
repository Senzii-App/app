"""Shifts DB — shifts CRUD and staff matching algorithm.
Direct port of src/db/shifts.rs including the full matching engine.
"""
import math
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.db.staff import Certification, AvailabilityWindow


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in km between two lat/lng pairs."""
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (math.sin(d_lat / 2.0) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2.0) ** 2)
    return r * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def proximity_score(staff_lat: float, staff_lng: float, site_lat: float, site_lng: float) -> float:
    """0 km → 1.0, ≥50 km → 0.0 (linear decay)."""
    km = haversine_km(staff_lat, staff_lng, site_lat, site_lng)
    return max(0.0, 1.0 - km / 50.0)


def skills_score(staff_certs: list[Certification], required_skills: list[str]) -> float:
    """Fraction of required skills matched by staff certifications. Case-sensitive."""
    if not required_skills:
        return 1.0
    cert_names = {c.name for c in staff_certs}
    match_count = sum(1 for s in required_skills if s in cert_names)
    return match_count / len(required_skills)


def _parse_time(s: str):
    """Parse 'HH:MM' or 'HH:MM:SS' to a time-like tuple (hour, minute, second)."""
    parts = s.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    sec = int(parts[2]) if len(parts) > 2 else 0
    return (h, m, sec)


def _time_to_str(t) -> str:
    """Format a time value as HH:MM."""
    if hasattr(t, 'hour'):
        return f"{t.hour:02d}:{t.minute:02d}"
    return str(t)


def _time_lt(a, b) -> bool:
    """Compare two time values (either datetime.time or (h,m,s) tuples)."""
    ah, am, _ = a if isinstance(a, tuple) else (a.hour, a.minute, a.second)
    bh, bm, _ = b if isinstance(b, tuple) else (b.hour, b.minute, b.second)
    return (ah, am) < (bh, bm)


def _time_le(a, b) -> bool:
    ah, am, _ = a if isinstance(a, tuple) else (a.hour, a.minute, a.second)
    bh, bm, _ = b if isinstance(b, tuple) else (b.hour, b.minute, b.second)
    return (ah, am) <= (bh, bm)


def _time_gt(a, b) -> bool:
    ah, am, _ = a if isinstance(a, tuple) else (a.hour, a.minute, a.second)
    bh, bm, _ = b if isinstance(b, tuple) else (b.hour, b.minute, b.second)
    return (ah, am) > (bh, bm)


def availability_score(
    availability: list[AvailabilityWindow],
    start_time: str,
    end_time: str,
    day_of_week: int,
) -> float:
    """Binary: 1.0 if shift overlaps any availability window on this day, 0.0 otherwise."""
    if not availability:
        return 0.0
    day_windows = [a for a in availability if a.day_of_week == day_of_week]
    if not day_windows:
        return 0.0
    shift_start = _parse_time(start_time)
    shift_end = _parse_time(end_time)
    overnight = _time_le(shift_end, shift_start)
    for win in day_windows:
        win_start = (win.start_time.hour, win.start_time.minute, win.start_time.second)
        win_end = (win.end_time.hour, win.end_time.minute, win.end_time.second)
        if overnight:
            overlaps = _time_lt(shift_start, win_end) or _time_gt(shift_end, win_start)
        else:
            overlaps = _time_lt(shift_start, win_end) and _time_gt(shift_end, win_start)
        if overlaps:
            return 1.0
    return 0.0


async def get_shift_by_id(conn: asyncpg.Connection, id: int, org_id: int) -> dict[str, Any] | None:
    """Get a single shift by ID, scoped to org."""
    row = await conn.fetchrow(
        """SELECT s.*, ws.name as site_name,
                  ws.latitude::double precision as site_lat,
                  ws.longitude::double precision as site_lng
           FROM shifts s
           JOIN work_sites ws ON ws.id = s.site_id
           WHERE s.id = $1 AND ws.organization_id = $2 AND s.deleted_at IS NULL""",
        id, org_id,
    )
    if row is None:
        return None
    skills = row["required_skills"] if row["required_skills"] else []
    return {
        "id": row["id"],
        "organization_id": row["organization_id"],
        "site_id": row["site_id"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "required_skills": skills,
        "min_staff": row["min_staff"],
        "assigned_staff_id": row["assigned_staff_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "site_name": row["site_name"],
        "site_lat": row["site_lat"],
        "site_lng": row["site_lng"],
    }


async def find_matching_staff(
    conn: asyncpg.Connection,
    shift_id: int,
    org_id: int,
    proximity_weight: float = 0.3,
    skills_weight: float = 0.4,
    availability_weight: float = 0.3,
) -> list[dict[str, Any]] | None:
    """Find and score available staff for a shift.
    Scoring: weighted sum of proximity + skills match + availability.
    Returns ALL staff (available + unavailable) sorted by score descending.
    """
    shift = await get_shift_by_id(conn, shift_id, org_id)
    if shift is None:
        return None

    rows = await conn.fetch(
        """SELECT
          s.id,
          s.name,
          s.email,
          s.latitude::double precision,
          s.longitude::double precision,
          s.timezone,
          COALESCE(cert.certs, '[]') as certifications,
          COALESCE(avail.windows, '[]') as availability
        FROM staff s
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object('name', name, 'expires_at', expires_at)) as certs
          FROM staff_certifications WHERE staff_id = s.id
        ) cert ON true
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
            'day_of_week', day_of_week,
            'start_time', start_time,
            'end_time', end_time
          )) as windows
          FROM staff_availability WHERE staff_id = s.id
        ) avail ON true
        WHERE s.organization_id = $1 AND s.deleted_at IS NULL""",
        org_id,
    )

    site_lat = shift["site_lat"] or 0.0
    site_lng = shift["site_lng"] or 0.0
    shift_start = shift["start_time"]
    shift_end = shift["end_time"]

    candidates = []
    for row in rows:
        tz = row["timezone"] or "UTC"
        if not tz:
            tz = "UTC"

        # Proximity score
        lat = row["latitude"]
        lng = row["longitude"]
        if lat is not None and lng is not None:
            prox = proximity_score(lat, lng, site_lat, site_lng)
        else:
            prox = 0.0

        # Skills score
        raw_certs = row["certifications"] or []
        certs = [Certification(name=c["name"], expires_at=c.get("expires_at")) for c in raw_certs]
        skill = skills_score(certs, shift["required_skills"])

        # Availability score — convert UTC shift times to staff's local timezone
        # Use Python's zoneinfo for timezone conversion
        try:
            from zoneinfo import ZoneInfo
            local_start = shift_start.astimezone(ZoneInfo(tz))
            local_end = shift_end.astimezone(ZoneInfo(tz))
        except Exception:
            local_start = shift_start
            local_end = shift_end

        day_of_week = local_start.weekday()  # Monday=0 .. Sunday=6
        # Convert to JS convention: 0=Sunday .. 6=Saturday
        day_of_week = (day_of_week + 1) % 7

        shift_start_str = f"{local_start.hour:02d}:{local_start.minute:02d}"
        shift_end_str = f"{local_end.hour:02d}:{local_end.minute:02d}"

        raw_avail = row["availability"] or []
        avail_windows = [
            AvailabilityWindow(
                day_of_week=w["day_of_week"],
                start_time=w["start_time"],
                end_time=w["end_time"],
            )
            for w in raw_avail
        ]
        avail = availability_score(avail_windows, shift_start_str, shift_end_str, day_of_week)

        raw_score = proximity_weight * prox + skills_weight * skill + availability_weight * avail

        # If shift requires skills and staff has zero matching certs, force score to 0
        if shift["required_skills"] and skill == 0.0:
            score = 0.0
        else:
            score = round(raw_score * 1000.0) / 1000.0

        days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        day_name = days[day_of_week] if 0 <= day_of_week < 7 else "?"

        candidates.append({
            "staff_id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "timezone": tz,
            "score": score,
            "breakdown": {
                "proximity_score": round(prox * 1000) / 1000,
                "skills_score": round(skill * 1000) / 1000,
                "availability_score": round(avail * 1000) / 1000,
            },
            "staff_location": {
                "latitude": lat,
                "longitude": lng,
            },
            "shift_local_time": {
                "day": day_name,
                "start": shift_start_str,
                "end": shift_end_str,
                "timezone": tz,
            },
            "certifications": [c.name for c in certs],
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates