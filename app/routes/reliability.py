from fastapi import APIRouter, HTTPException
from app.reliability.state import state
from app.observability.logger import get_logger

router = APIRouter(prefix="/reliability")
logger = get_logger("brp.reliability")

@router.post("/toggle-latency")
def toggle_latency():
    state.latency_enabled = not state.latency_enabled
    adding = state.latency_ms if state.latency_enabled else 0
    logger.info("latency_injection_toggled", extra={"enabled": state.latency_enabled, "adding_ms": adding})
    return {"latency_injection": state.latency_enabled, "adding_ms": adding}


@router.post("/toggle-errors")
def toggle_errors():
    state.errors_enabled = not state.errors_enabled
    logger.info("error_injection_toggled", extra={"enabled": state.errors_enabled})
    return {"error_injection": state.errors_enabled}


@router.get("/status")
def reliability_status():
    return {
        "latency_injection": state.latency_enabled,
        "adding_ms": state.latency_ms if state.latency_enabled else 0,
        "error_injection": state.errors_enabled,
    }


@router.get("/trigger-error")
def trigger_error():
    logger.error("manual_error_triggered")
    raise HTTPException(status_code=500, detail="manually triggered error")