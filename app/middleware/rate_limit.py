"""Rate limiting — in-memory IP-based sliding window.
Direct port of src/middleware/rate_limit.rs.
"""
import time
from collections import defaultdict
from fastapi import Request, HTTPException


class RateLimiter:
    """Sliding window rate limiter per client IP."""

    def __init__(self, max_requests: int = 10, window_secs: int = 60):
        self.max_requests = max_requests
        self.window_secs = window_secs
        self._timestamps: dict[str, list[float]] = defaultdict(list)

    def check(self, ip: str) -> int | None:
        """Returns None if allowed, retry_after_secs if rate limited."""
        now = time.time()
        timestamps = self._timestamps[ip]
        # Remove timestamps outside the sliding window
        timestamps[:] = [t for t in timestamps if now - t < self.window_secs]

        if len(timestamps) >= self.max_requests:
            oldest = timestamps[0]
            elapsed = now - oldest
            retry_after = max(1, int(self.window_secs - elapsed))
            return retry_after

        timestamps.append(now)
        return None


def get_client_ip(request: Request) -> str:
    """Extract client IP from X-Forwarded-For or X-Real-IP."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return "0.0.0.0"