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

    #Prometheus runs bare metal on BOTH nodes (standard Prom HA), so /slo always
    #reads this node's own instance and never the peer's. Used by /slo
    #(fleet-wide view) — deliberately NOT by /health.
    #
    #The default is loopback for running the app OUTSIDE a container, where it
    #is correct. On the nodes it is overridden to host.docker.internal:9090 via
    #.env, because in a container 127.0.0.1 is the container's own loopback and
    #Prometheus is on the host — see .env.example. A wrong value here does not
    #fail loudly: /slo returns 200 with status "unavailable", which is the
    #honest answer and easy to mistake for Prometheus being down.
    PROM_URL: str = os.getenv("PROM_URL", "http://127.0.0.1:9090")


settings = Settings()
