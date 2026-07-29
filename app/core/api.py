from fastapi import FastAPI

from app.routes.health import router as health_router
from app.routes.metrics import router as metrics_router
from app.routes.root import router as root_router
from app.routes.slo import router as slo_router

from app.observability.middleware import PrometheusMiddleware
from app.observability.metrics import APP_INFO, SYSTEM_HEALTH

from app.core.settings import settings

from app.routes.reliability import router as reliability_router


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
    )

    APP_INFO.labels(
        service="brp-api",
        node=settings.NODE_NAME,
        version=settings.APP_VERSION,
    ).set(1)
    SYSTEM_HEALTH.set(1)

    app.add_middleware(PrometheusMiddleware)

    app.include_router(root_router)
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(slo_router)
    app.include_router(reliability_router)

    return app


app = create_app()
