# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — 2026-07-29

Initial port. The FastAPI service, Prometheus metrics, SLO definitions, failure
injection, and k6 tests come from
[cloud-reliability-platform](https://github.com/ericratz/cloud-reliability-platform);
everything below is what changed to run them highly-available on two bare-metal
nodes instead of on AKS.

### Infrastructure

- Two identical GIADA KabyLake mini-PCs (Intel i5-7200U, ~4GB RAM, 2TB 5400rpm
  HDD). The original plan assumed mismatched hardware (i5 vs Celeron); the nodes
  are in fact identical down to the CPU and disk model. The project's thesis
  moved accordingly: **the OS is the only variable** (Ubuntu 26.04 vs Rocky
  10.2), so any per-node latency difference is attributable to the OS, not to
  capacity.
- Both nodes given node-side static IPs high in the subnet (node1
  192.168.71.251, node2 192.168.71.252) rather than left on DHCP leases, so the
  addresses other services point at cannot move. VIP 192.168.71.250. LAN is a
  /22, not the /24 the addresses suggest.

### Removed

- **Kubernetes client dependency.** `app/routes/health.py` imported the
  `kubernetes` package to read a pod's restart count via
  `load_incluster_config()`. Off-cluster it was a hard import that always
  returned `-1`. Replaced by process `uptime_seconds`.
- Terraform / Azure provisioning, Kubernetes manifests, Grafana dashboards and
  Loki config, HPA. Rationale for each is in README, "What was cut, and why".

### Changed

- **`/health` no longer queries Prometheus.** It gates a node's return to the
  load balancer pool; a liveness check must not depend on a remote system it is
  meant to be independent of. Now node-local, no network calls, ~15ms. Fleet SLO
  numbers stay on `/slo`.
- **`/slo` degrades honestly.** `safe_query` returned `0.0` both when Prometheus
  was unreachable and when a query matched no series, so an unreachable
  Prometheus rendered as `availability_percent: 0.0` — a healthy system claiming
  a total outage. The cases are now distinct: unreachable → `status:
  "unavailable"`; empty result → per-metric default (availability 1.0, not 0.0).
  `/slo` still returns 200 when unavailable, so an observability outage is not
  mistaken for a serving outage.
- `PROM_URL` default moved off the Kubernetes service DNS name to
  `127.0.0.1:9090` — Prometheus runs on both nodes, so `/slo` never crosses the
  network. Query timeout 5s → 2s.
- Metric prefix `crp_` → `brp_`. `brp_app_info` gained `node`/`version` labels;
  per-node distinction otherwise comes from the Prometheus target label.
- Every log line stamped with `node`/`version`; logger `propagate` disabled
  (uvicorn's root logger was double-emitting each line).
- `/health` intentionally *not* exempt from failure injection, so an injected
  outage makes the node genuinely look unhealthy. `/metrics` stays exempt so
  Prometheus keeps scraping through the window under test.

### Added

- **High availability via keepalived.** Both nodes run nginx + a VRRP-floated
  VIP; failover is automatic and health-gated. Both instances are `state
  BACKUP` with different priorities (not MASTER/BACKUP), because `nopreempt` is
  ignored on a MASTER — otherwise a recovering node preempts the VIP and causes
  a second outage. The health check (`check_nginx.sh`) hits `/nginx-health`,
  served by nginx and never proxied — not `pgrep` (a wedged nginx still has a
  process) and not the proxied `/health` (which would flap the VIP between two
  broken LBs when the app tier is down). `notify.sh` logs every transition with
  a millisecond timestamp so failover can be measured against the request log.
- **Redundant Prometheus** — one per node, both scraping both targets (standard
  Prometheus HA). Enables the local-only `PROM_URL` above.
- `/nginx-health` endpoint in the nginx config, served locally for the failover
  probe.
- Nginx weighted upstream (equal 1:1 weights — a measured result on identical
  hardware, see README) split into `brp-upstream.conf` so a rolling update edits
  one small file. `proxy_next_upstream` retry — the actual zero-downtime
  mechanism. Access log records `$upstream_addr`/`$upstream_status` so it can
  show which node served each request and whether a retry occurred.
- Prometheus scrape config targeting both nodes directly (not through the LB,
  which would alternate one series between machines), labeled by `node` and
  `os`. Nine recording/alerting rules: scrape liveness, full-fleet outage, error
  rate, p95 latency, per-node latency skew.
- `scripts/pool.sh` — add/remove a node from the upstream with a pre-reload
  syntax check, backup/restore on failure, and a guard against draining the pool
  empty.
- `scripts/watch-uptime.sh` — per-request evidence log (timestamp, status, node
  attribution); exits non-zero if any request failed.
- `k6/rolling-update.js` — `constant-arrival-rate` scenario,
  `http_req_failed: rate==0` (open model, so offered load does not drop when a
  node goes away). `k6/baseline.js` reports the observed per-node split so
  weights are set from measurement.
- Container healthcheck (stdlib `urllib`, no `curl` in the image), non-root
  user, `APP_VERSION` build arg. Compose file refuses to start without
  `NODE_NAME` rather than defaulting to `unknown`.
- `RUNBOOK.md` (deployment, rolling update, failover drills, per-OS firewall +
  SELinux steps), architecture and rationale in `README.md`.

### Fixed

Both found by testing the tooling against a deliberately injected outage:

- `watch-uptime.sh` extracted the node name with a pattern matching only
  `"node":"x"` and not `"node": "x"`, silently degrading attribution to `-`. Now
  whitespace-tolerant, and unattributed successful responses are reported rather
  than passing quietly.
- `watch-uptime.sh` parsed the response body and `curl -w` metadata from one
  stdout stream, which could write a duration into the status-code column. Body
  now goes to a temp file so stdout carries only the metadata.

### Known gaps

Not yet deployed to the nodes; node2 Podman Quadlet unit, per-OS firewall/SELinux
config, and the health-monitor v3.0 timer are pending; the rolling update and
VIP failover have not yet been executed and measured on real hardware.
