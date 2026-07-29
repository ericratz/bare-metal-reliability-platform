import os

from pydantic import BaseModel


class Settings(BaseModel):
    APP_NAME: str = "Bare-Metal Reliability Platform"
    APP_VERSION: str = os.getenv("APP_VERSION", "dev")
    METRICS_PREFIX: str = "brp"

    #identity of the physical node this instance runs on. Set per-node in
    #docker-compose so the load balancer, health monitor, and rolling-update
    #runbook can all tell the two instances apart.
    NODE_NAME: str = os.getenv("NODE_NAME", "unknown")

    #Prometheus runs bare metal on BOTH nodes (standard Prom HA), so this is
    #always local — /slo never crosses the network. Used by /slo (fleet-wide
    #view) — deliberately NOT by /health.
    PROM_URL: str = os.getenv("PROM_URL", "http://127.0.0.1:9090")


settings = Settings()
