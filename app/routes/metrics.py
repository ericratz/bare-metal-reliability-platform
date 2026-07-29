from fastapi import APIRouter
from prometheus_client import generate_latest
from starlette.responses import Response
from app.observability.metrics import REGISTRY

router = APIRouter()

@router.get("/metrics")
def metrics():
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain",
    )