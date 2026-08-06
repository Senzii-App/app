"""Server metrics — Prometheus-style metrics for the admin dashboard.
Direct port of src/db/server_metrics.rs.
"""
import psutil
import time
from collections import defaultdict

# Simple in-memory counters
_request_counts: dict[str, int] = defaultdict(int)
_start_time = time.time()


def record_request(path: str):
    """Record a request for metrics."""
    _request_counts[path] += 1


def render_metrics() -> str:
    """Render Prometheus-format metrics."""
    lines = []
    uptime = time.time() - _start_time

    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()

    lines.append("# HELP senzii_uptime_seconds Server uptime in seconds")
    lines.append("# TYPE senzii_uptime_seconds counter")
    lines.append(f"senzii_uptime_seconds {uptime:.0f}")

    lines.append("# HELP senzii_cpu_percent CPU usage percentage")
    lines.append("# TYPE senzii_cpu_percent gauge")
    lines.append(f"senzii_cpu_percent {cpu}")

    lines.append("# HELP senzii_memory_percent Memory usage percentage")
    lines.append("# TYPE senzii_memory_percent gauge")
    lines.append(f"senzii_memory_percent {mem.percent}")

    lines.append("# HELP senzii_memory_used_bytes Memory used in bytes")
    lines.append("# TYPE senzii_memory_used_bytes gauge")
    lines.append(f"senzii_memory_used_bytes {mem.used}")

    lines.append("# HELP senzii_requests_total Total requests per path")
    lines.append("# TYPE senzii_requests_total counter")
    for path, count in _request_counts.items():
        safe_path = path.replace("/", "_").strip("_") or "root"
        lines.append(f'senzii_requests_total{{path="{path}"}} {count}')

    return "\n".join(lines) + "\n"