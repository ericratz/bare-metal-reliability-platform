from fastapi import APIRouter

from app.core.settings import settings
from app.observability.prom_slo import (
    get_availability,
    get_error_rate,
    get_p95_latency,
)

router = APIRouter()


@router.get("/slo")
def slo():
    """
    Fleet-wide SLO view, computed by Prometheus across both nodes.

    Unlike /health this is not node-local: it queries node1's Prometheus, so
    both instances return the same numbers. When Prometheus is unreachable the
    response degrades to status "unavailable" rather than reporting zeros.

    Returns 200 even when unavailable, on purpose. A 5xx here would make an
    unreachable Prometheus look like an application fault to anything watching
    the load balancer, and would show up as failed requests in the k6 baseline
    — an observability outage is not a serving outage.
    """
    availability = get_availability()
    error_rate   = get_error_rate()
    latency      = get_p95_latency()

    if availability is None or error_rate is None or latency is None:
        return {
            "status": "unavailable",
            "reason": "prometheus unreachable",
            "prometheus_url": settings.PROM_URL,
            "node": settings.NODE_NAME,
        }

    return {
        "status": "ok",
        "node": settings.NODE_NAME,
        "window": "2m",
        "availability_percent": round(availability * 100, 2),
        "error_rate_percent": round(error_rate * 100, 2),
        "p95_latency_ms": round(latency * 1000, 2),
    }
