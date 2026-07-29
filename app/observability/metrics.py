from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, disable_created_metrics

disable_created_metrics()

REGISTRY = CollectorRegistry()

#Metric names carry the brp_ prefix (was crp_ in the cloud version). No node
#label is attached here on purpose — Prometheus already distinguishes the two
#instances via the target's `node` label (see deploy/prometheus/prometheus.yml),
#and duplicating it in-app would double the series cardinality for no gain.
REQUEST_COUNT = Counter(
    "brp_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
    registry=REGISTRY
)

REQUEST_LATENCY = Histogram(
    "brp_request_duration_seconds",
    "Request latency in seconds",
    ["endpoint", "status"],
    registry=REGISTRY
)

ERROR_COUNT = Counter(
    "brp_errors_total",
    "Total application errors",
    ["endpoint"],
    registry=REGISTRY
)

#single low-cardinality series, so node/version are worth carrying as labels
APP_INFO = Gauge(
    "brp_app_info",
    "Application metadata",
    ["service", "node", "version"],
    registry=REGISTRY
)

SYSTEM_HEALTH = Gauge(
    "brp_system_health",
    "Overall system health status",
    registry=REGISTRY
)
