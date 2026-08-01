from fastapi import APIRouter

from app.core.settings import settings
from app.observability.prom_slo import (
    NO_DATA,
    get_availability,
    get_p95_latency,
)

router = APIRouter()


def _scaled(value, factor):
    """NO_DATA becomes an explicit null; a real reading is scaled and rounded."""
    if value is NO_DATA:
        return None
    return round(value * factor, 2)


@router.get("/slo")
def slo():
    """
    Fleet-wide SLO view, computed by Prometheus across both nodes.

    Unlike /health, the *numbers* are not node-local — each is a sum across
    both nodes' scrape targets. The *query* is: every node asks its own
    Prometheus, never the peer's, so there is no instance whose loss takes /slo
    down fleet-wide. The two instances scrape the same targets, so they should
    agree; if they disagree, one of them has a scrape failing, and that is worth
    knowing rather than papering over with a single shared source of truth.

    When Prometheus is unreachable the response degrades to status
    "unavailable" rather than reporting zeros.

    Returns 200 even when unavailable, on purpose. A 5xx here would make an
    unreachable Prometheus look like an application fault to anything watching
    the load balancer, and would show up as failed requests in the k6 baseline
    — an observability outage is not a serving outage.

    A metric Prometheus has no series for is reported as null and named in
    `no_data`, never as a plausible number. An idle window and a perfect window
    are different claims and the response has to be able to tell them apart.

    There is no error_rate_percent, and re-adding one would be a regression.
    The query behind it was the complement of the availability query over
    identical terms, so the field was `100 - availability_percent` — the same
    measurement twice, at the cost of a second Prometheus round trip per
    request. Derive it if you want it. Independent rounding also meant the two
    could disagree in the last decimal, which invited reading agreement between
    them as corroboration when nothing was being corroborated.

    `status` stays "ok" whenever Prometheus answered, including when every
    metric is null. It reports whether this endpoint could do its job, not
    whether traffic happened to be flowing — the health monitor gates on it,
    and a quiet night is not a fault.
    """
    availability = get_availability()
    latency      = get_p95_latency()

    if availability is None or latency is None:
        return {
            "status": "unavailable",
            "reason": "prometheus unreachable",
            "prometheus_url": settings.PROM_URL,
            "node": settings.NODE_NAME,
        }

    measured = {
        "availability_percent": _scaled(availability, 100),
        "p95_latency_ms":       _scaled(latency, 1000),
    }

    body = {
        "status": "ok",
        "node": settings.NODE_NAME,
        "window": "2m",
        **measured,
    }

    absent = [name for name, value in measured.items() if value is None]
    if absent:
        #named rather than left to be inferred from the nulls, so "no traffic in
        #the window" is something the response says out loud
        body["no_data"] = absent

    return body
