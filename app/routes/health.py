import time

import psutil
from fastapi import APIRouter

from app.core.settings import settings

router = APIRouter()

#health thresholds
MEMORY_WARN = 70 #percent
MEMORY_CRIT = 90
DISK_WARN   = 70
DISK_CRIT   = 90

#process start time. Resets when the container restarts, which is how the
#rolling-update runbook confirms a node actually took the new image.
_STARTED_AT = time.time()


def classify(value, warn, crit):
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "ok"


def worst(statuses):
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    return "healthy"


def check_memory():
    #psutil reads /proc/meminfo, which inside a container is the host's memory,
    #not a cgroup limit. That is the number we want here — these are dedicated
    #single-purpose nodes — but it means this is host scope, not container scope.
    m = psutil.virtual_memory()
    return {
        "status": classify(m.percent, MEMORY_WARN, MEMORY_CRIT),
        "used_mb": round(m.used / 1024**2, 1),
        "total_mb": round(m.total / 1024**2, 1),
        "percent": m.percent,
        "scope": "host",
    }


def check_disk():
    d = psutil.disk_usage("/")
    return {
        "status": classify(d.percent, DISK_WARN, DISK_CRIT),
        "used_gb": round(d.used / 1024**3, 2),
        "total_gb": round(d.total / 1024**3, 2),
        "percent": d.percent,
        "scope": "container",
    }


@router.get("/health")
def health():
    """
    Local liveness check. Answers "is this instance serving?" using only
    on-box state — no network calls, no dependency on Prometheus or on the
    other node.

    This is deliberate. /health is what the rolling-update runbook curls to
    decide whether a node is safe to return to the Nginx pool, and what the
    health monitor polls. Reaching across the LAN to node1's Prometheus to
    answer it would make node2 report unhealthy whenever node1 is down —
    coupling the health signal to the very thing it exists to be independent
    of. Fleet-wide SLO numbers live on /slo instead.
    """
    memory = check_memory()
    disk   = check_disk()

    return {
        "status": worst([memory["status"], disk["status"]]),
        "service": "brp-api",
        "node": settings.NODE_NAME,
        "version": settings.APP_VERSION,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "checks": {
            "api": "running",
            "memory": memory,
            "disk": disk,
        },
    }
