import requests

from app.core.settings import settings
from app.observability.logger import get_logger

logger = get_logger("brp.slo")


def safe_query(promql: str, default: float = 0.0):
    """
    Runs an instant query against Prometheus.

    Returns:
      float  - the query result
      default- the query succeeded but matched no series (e.g. no traffic yet)
      None   - Prometheus is unreachable or the query failed

    The default/None split matters. The original version returned 0.0 for both
    cases, which made "Prometheus is down" and "no requests have been served"
    indistinguishable from "availability is 0%" — reporting a total outage when
    nothing was actually wrong. Callers now decide what an absent series means
    per metric, and surface unreachable Prometheus as unavailable rather than
    as a fabricated number.
    """
    try:
        resp = requests.get(
            f"{settings.PROM_URL}/api/v1/query",
            params={"query": promql},
            #short: /slo is polled by the health monitor and hit by load tests,
            #so a hung Prometheus must not hold the request open.
            timeout=2,
        )
        resp.raise_for_status()
        result = resp.json().get("data", {}).get("result", [])

        if not result:
            return default

        value = result[0].get("value", [None, "0"])[1]
        return float(value)

    except Exception as e:
        logger.error("slo_query_failed", extra={"error": str(e), "query": promql})
        return None


def get_availability():
    query = """
    1 - (
        sum(rate(brp_requests_total{status=~"5.."}[2m]))
        /
        clamp_min(sum(rate(brp_requests_total[2m])), 1)
    )
    """
    #no series yet means nothing has failed yet
    return safe_query(query, default=1.0)


def get_error_rate():
    query = """
    sum(rate(brp_requests_total{status=~"5.."}[2m]))
    /
    clamp_min(sum(rate(brp_requests_total[2m])), 1)
    """
    return safe_query(query, default=0.0)


def get_p95_latency():
    query = """
    histogram_quantile(
        0.95,
        sum(rate(brp_request_duration_seconds_bucket[2m])) by (le)
    )
    """
    return safe_query(query, default=0.0)
