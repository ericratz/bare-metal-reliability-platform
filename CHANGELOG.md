# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **Podman silently dropped the container healthcheck.** `podman build` defaults
  to the OCI image format, which has no healthcheck field, so the Dockerfile's
  `HEALTHCHECK` was discarded with nothing but a build warning:
  `HEALTHCHECK is not supported for OCI image format and will be ignored`.
  node1's Docker build kept it and reported `Up (healthy)`; node2's container
  had no health gating whatsoever. One Dockerfile, one build-arg, two
  materially different artifacts. Now built with `--format docker` in
  `RUNBOOK.md` §1b, §2 step 4, and `deploy/podman/README.md`.

  The third instance of the OS-is-the-variable thesis after the two firewall
  findings, and the first where the divergence is in the *artifact* rather than
  the host plumbing — which is the case the project's central question was
  actually asking about. It is also the quietest: nothing at runtime reports
  that a container has no healthcheck, so the gap is visible only in build
  output nobody re-reads.
- **`localhost` does not reach the app on node2.** Rootless Podman forwards
  with pasta, which binds the wildcard and accepts on `::1`, while the
  container's uvicorn listens on `0.0.0.0` only — so a v6 connection completes
  its TCP handshake and is then reset with nothing behind it. Rocky resolves
  `localhost` to `::1` first, and because the *connect* succeeded the client
  does not fall back to `127.0.0.1`; `curl -s localhost:8000/health` returns an
  empty body and exits, which reads as a dead app while it is serving 200s on
  the v4 literal and on its LAN address.

  `PublishPort` in `deploy/podman/brp-api.container` is now
  `0.0.0.0:8000:8000`, so `::1` refuses cleanly and clients fall back; the
  verification commands address `127.0.0.1` explicitly rather than relying on
  that fallback. Nothing in the platform needed v6 ingress — the nginx
  upstream, the Prometheus targets, `PROM_URL` and the health monitor's
  endpoints are all v4 literals, which is why this stayed invisible.

  Same shape as the healthcheck finding: node1 runs the identical image over
  `localhost` without complaint, because Docker's userland proxy publishes on
  both families and bridges v6 to the container's v4 socket. Docker hides the
  mismatch; Podman surfaces it. A connect-then-reset is the worse failure of
  the two — a clean refusal is retried, a reset is not.
- **`APP_VERSION` must be set in `.env` on node2, not only as a build-arg.**
  The app reads the version from its runtime environment
  (`app/core/settings.py`), and the Quadlet's `EnvironmentFile` injects `.env`
  into the container — which overrides the `ENV APP_VERSION` the build-arg
  baked in. With the shipped `.env.example` default, a build tagged `0.1.0`
  reported `"dev"`.

  node1 has no such split: compose feeds one `.env` value to the build-arg, the
  image tag and the runtime env at once. The consequence was in §2 step 4,
  which rebuilt with a new build-arg and then gated the return-to-pool on step
  5 seeing a new `version` — a check that could never have fired, on the one
  signal that proves a rolling update landed. Both steps now bump `.env` first.

- **VIP moved from `192.168.71.250` to `192.168.71.245`.** `.250` was already
  held by an appliance at `d8:44:89:a0:66:60` — it answers ARP and 404s
  `/health`, which is why early monitor runs reported the VIP unreachable. The
  0.1.0 entry below records `.250` as chosen and is left as written; this is
  the correction, not a retcon. `.245` verified unanswered via `ip neigh` from
  both nodes, and sits in the same high-subnet static band as `.251`/`.252`.
- `deploy/nginx/brp.conf` now documents at length why `/health` is **not**
  special-cased. Pinning it to the local app was written and then reverted
  before shipping: every evidence tool in this repo attributes a request to a
  node by reading the `node` field out of a `/health` response, so pinning
  reduces `watch-uptime.sh`, `k6/baseline.js` and `k6/rolling-update.js` to a
  single node — silently, since the field is still present and merely never
  changes. Bypassing the upstream also disables `proxy_next_upstream`, which
  is the actual zero-downtime mechanism, so a node drained by `pool.sh` would
  keep answering `/health` from its own restarting container and manufacture
  the dropped requests `rolling-update.js` exists to detect.

  The concern behind the idea is legitimate and belongs one layer down: a
  consumer asking "is THIS node healthy" must ask the app directly on `:8000`,
  not through the load balancer whose job is to hide which node answered.
  "node-local" in the README endpoint table describes the data, not the
  routing.
- `location = /report` serves the health monitor's HTML report as a static
  file from `/var/www/health/`, allow-listed to the nodes rather than the LAN.
  Not proxied: a report about a node's health must not depend on that node's
  app tier, and through the upstream it could be answered by the other node.

  The directory is created setgid — `root:www-data` on node1, `root:nginx` on
  node2, mode `2750`. The monitor writes the report `0640` so local accounts
  cannot read it, which means nginx needs group access, and since the file is
  rewritten every cycle a one-off `chgrp` does not survive. Wrong group gives
  a 403 rather than a 404, which distinguishes it from "not yet written."
- **`RUNBOOK.md` §1a: VRRP cannot be opened with `ufw`.** The node1 block ran
  `ufw allow from <peer> proto vrrp`, which ufw rejects — its `proto` keyword
  accepts only `ah`, `esp`, `gre`, `igmp`, `ipv6`, `tcp`, `udp`, with no
  numeric-protocol form. Replaced with an `ufw-before-input -p 112` rule in
  `/etc/ufw/before.rules`, plus the `iptables -S` check that is the only way to
  confirm it — `ufw status` does not list `before.rules` content.

  Recorded as a portability finding rather than a typo: firewalld expresses
  this in one flag and ufw cannot express it at all. It is the sharpest
  instance so far of the thesis that the OS is the variable, and the failure it
  would have produced — blocked adverts, both nodes claiming the VIP — is the
  one §1a's own warning calls the most common keepalived bring-up failure.
- **`RUNBOOK.md` §1a enabled ufw on node1 with no SSH rule.** ufw defaults to
  deny-incoming and ships with nothing allowed, so `ufw --force enable` closed
  port 22 on a headless, minimized box — and `--force` is precisely what
  suppresses ufw's own "may disrupt existing ssh connections" prompt. The
  failure is delayed rather than immediate: the current session survives on the
  ESTABLISHED rule, so the lockout only appears at the next login, when
  physical access is the only remedy. Now allows `22/tcp` before enabling, with
  a verification step to run while a working session is still open.

  The same OS-variable pattern as the VRRP finding, inverted: firewalld ships
  `ssh` permitted in its default zone, so node2 was never exposed to this. The
  two firewalls disagree about what a fresh install should permit, and the
  runbook had been written as though they agreed.

### Added

- `RUNBOOK.md` §1 now registers each component with the health monitor as part
  of the step that deploys it. `HEALTH_SERVICES` and `HEALTH_APP_ENDPOINTS`
  start trimmed to what exists, so the monitor's exit code is meaningful from
  the first run instead of sitting at a standing WARNING until the platform is
  finished. The tradeoff is stated in place: an unregistered component is
  unwatched and nothing reports that fact.
- `deploy/systemd/health-monitor-node{1,2}.conf` — per-node drop-ins for
  `health-monitor.service.d/10-fleet.conf`, filling a directory that was empty.
  Installed with only the baseline entries active; every later entry is
  pre-written, commented, and tagged with the `RUNBOOK.md` step that enables
  it, so registration is "move the active line down one" rather than composing
  a config from memory four separate times. A drop-in rather than a unit edit,
  because the unit is replaced on the monitor's next install.

  node2's carries the trap explicitly: `brp-api` must **not** go in
  `HEALTH_SERVICES`, since a rootless Quadlet user unit is invisible to
  system-level `systemctl --failed` and would register as permanently
  not-installed — a standing WARNING that never clears.

  Both files, and `RUNBOOK.md` §1, carry a **drop-in collision check**. The
  monitor's repo ships its own per-node drop-ins for the same unit, systemd
  merges every `*.conf` in the directory, and last writer wins in
  lexicographic order — where digits precede letters, so `10-fleet.conf` loses
  to `nodeN.conf`. The failure is silent: the file you edited is present,
  correct, and ignored, and the trim is quietly replaced by the shipped end
  state. Resolved by checking the merged result
  (`systemctl show -p Environment`) rather than reasoning about filenames,
  since a rename in either repo would otherwise flip precedence unannounced.

### Known gaps

- **node2's app logs may not be reachable via `journalctl`.**
  `journalctl --user -u brp-api` returns "No journal files were found" on node2
  even though the unit is running and its systemd messages are visible through
  `systemctl --user status`. `LogDriver=journald` was chosen in the Quadlet
  specifically so the health monitor's `journalctl -t brp-api` check could read
  app logs beside OS logs; if the user journal is absent, that check has
  nothing to read on node2 while working on node1 — another divergence, and one
  that would present as an empty result rather than an error. `podman logs
  brp-api` works and is the fallback. Not yet diagnosed; suspect journald
  storage or user-journal configuration on Rocky rather than the unit.

- **`.245` is confirmed free at layer 2 only.** The router's admin UI is not
  available, so it cannot be proven outside the DHCP pool. Accepted on the
  evidence that `.250`/`.251`/`.252` already coexist with DHCP at the top of
  the `/22`, and on RFC 2131 §4.3.1 — a conforming server probes before
  offering, and keepalived answers ARP continuously, so the address defends
  itself while the platform is up. The exposure window is both nodes being
  down simultaneously for long enough to be leased away.

  This is mitigated by detection, not prevention: the health monitor's
  `answered_by` on the VIP names the responding MAC every cycle, so a
  collision surfaces in one interval rather than as months of intermittent
  symptoms. `RUNBOOK.md` §1e records both nodes' MACs so that check is
  actionable. Revisit if router access is ever obtained.
- **The report path is set by overriding `ExecStart` in the drop-in**, which
  pins the whole command line — a future upstream change to the monitor's
  `ExecStart` would be silently discarded. Every other setting in the drop-in
  is an `Environment=` line and has no such problem. The fix belongs on the
  monitor's side: an env var for the report path would let that block be
  deleted. Until then, re-diff the drop-in against `systemctl cat` after any
  monitor upgrade.

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
