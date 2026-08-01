import math

import requests

from app.core.settings import settings
from app.observability.logger import get_logger

logger = get_logger("brp.slo")


class _NoData:
    """Sentinel for "Prometheus answered, and there is nothing to report"."""

    def __repr__(self):
        return "NO_DATA"

    #falsy so an accidental `if value:` cannot mistake it for a real reading
    def __bool__(self):
        return False


NO_DATA = _NoData()


def safe_query(promql: str):
    """
    Runs an instant query against Prometheus.

    Returns:
      float   - the query result
      NO_DATA - the query succeeded but matched no series (e.g. no traffic yet)
      None    - Prometheus is unreachable or the query failed

    Three outcomes, and callers must not collapse them back into two. This
    function used to take a per-metric `default` and return it on an empty
    result, which meant get_availability() answered 1.0 when nothing had been
    served at all — indistinguishable, in the response body, from a measured
    100%. That is the number /slo reported for the entire deploy, on both
    nodes, while carrying no traffic. A fabricated SLO is worse than an absent
    one: an absent one prompts a question, a fabricated one reads as evidence.

    NaN is treated as no data too. histogram_quantile over a histogram with no
    observations returns NaN, and float('nan') survives all the way into the
    response, where the JSON encoder emits a bare `NaN` token — which is not
    valid JSON. Every strict parser downstream, jq included, rejects the whole
    document rather than the one field, so a quiet window would have taken out
    /slo for anything parsing it.
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
            return NO_DATA

        value = result[0].get("value", [None, None])[1]
        if value is None:
            return NO_DATA

        value = float(value)
        #Prometheus renders these as the strings "NaN", "+Inf" and "-Inf", which
        #float() accepts without complaint, so this has to be checked after the
        #conversion rather than before it.
        if not math.isfinite(value):
            return NO_DATA

        return value

    except Exception as e:
        logger.error("slo_query_failed", extra={"error": str(e), "query": promql})
        return None


#`or vector(0)` on the 5xx term, and it is load-bearing rather than defensive.
#sum(rate()) over a selector that matches nothing yields an empty vector, not a
#zero, and empty propagates through the arithmetic — so with real traffic and
#no errors the whole expression returned nothing, and the old default=1.0 was
#quietly standing in for the healthy case as well as the idle one. Substituting
#0 for the missing error term leaves exactly one thing that can empty the
#expression: no requests at all, which is the case NO_DATA is for.
#
#clamp_min stays, but note what it does: under 1 req/s it inflates the
#denominator and understates the error rate. §2 runs at 20 req/s, well clear of
#that floor.
def get_availability():
    query = """
    1 - (
        (sum(rate(brp_requests_total{status=~"5.."}[2m])) or vector(0))
        /
        clamp_min(sum(rate(brp_requests_total[2m])), 1)
    )
    """
    return safe_query(query)


#There is deliberately no get_error_rate(). It was `1 - get_availability()`
#computed by asking Prometheus the same question a second time — a second
#network round trip, on an endpoint the health monitor polls, for a number the
#caller can already derive. /slo drops the field; Prometheus keeps its own
#`brp:error_rate:ratio5m` recording rule, which is what the alerts fire on and
#is unaffected by any of this.
def get_p95_latency():
    query = """
    histogram_quantile(
        0.95,
        sum(rate(brp_request_duration_seconds_bucket[2m])) by (le)
    )
    """
    return safe_query(query)
