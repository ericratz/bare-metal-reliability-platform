from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.settings import settings

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    base = str(request.base_url).rstrip("/")
    return f"""
    <html>
    <head><title>{settings.APP_NAME}</title></head>
    <body>
        <h2>{settings.APP_NAME}</h2>
        <p>Served by <strong>{settings.NODE_NAME}</strong> &mdash; version {settings.APP_VERSION}</p>
        <ul>
            <li><a href="{base}/health">Health</a> (node-local)</li>
            <li><a href="{base}/metrics">Metrics</a></li>
            <li><a href="{base}/slo">SLO</a> (fleet-wide, via Prometheus)</li>
            <li><a href="{base}/reliability/status">Reliability Status</a></li>
            <li><a href="{base}/docs">API Docs (Swagger)</a></li>
        </ul>
    </body>
    </html>
    """
