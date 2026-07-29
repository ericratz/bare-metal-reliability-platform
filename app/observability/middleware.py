import time
import asyncio
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from app.observability.metrics import REQUEST_COUNT, REQUEST_LATENCY, ERROR_COUNT
from app.observability.logger import get_logger
from app.reliability.state import state

logger = get_logger("brp.request")

#/health is deliberately NOT skipped: when failure injection is on, the node
#must actually look unhealthy, otherwise the failure-injection test can't prove
#the health monitor and the load balancer notice. /metrics stays exempt so
#Prometheus keeps scraping through an injected outage — losing the metrics for
#the exact window you are testing would defeat the point.
SKIP_INJECTION = {
    "/metrics",
    "/reliability/toggle-latency",
    "/reliability/toggle-errors",
    "/reliability/status"
}

class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        endpoint = request.url.path
        method = request.method
        status_code = 500

        try:
            if endpoint not in SKIP_INJECTION:
                if state.errors_enabled:
                    status_code = 500
                    return JSONResponse({"detail": "injected error"}, status_code=500)
                if state.latency_enabled:
                    await asyncio.sleep(state.latency_ms / 1000)

            response = await call_next(request)
            status_code = response.status_code
            return response

        except Exception:
            try:
                ERROR_COUNT.labels(endpoint=endpoint).inc()
            except Exception:
                pass
            raise

        finally:
            duration = time.time() - start

            logger.info("request", extra={
                "method": method,
                "endpoint": endpoint,
                "status": status_code,
                "duration_ms": round(duration * 1000, 2),
            })

            try:
                REQUEST_LATENCY.labels(endpoint=endpoint, status=str(status_code)).observe(duration)
            except Exception:
                pass

            try:
                REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=str(status_code)).inc()
            except Exception:
                pass

            if status_code >= 500:
                try:
                    ERROR_COUNT.labels(endpoint=endpoint).inc()
                except Exception:
                    pass