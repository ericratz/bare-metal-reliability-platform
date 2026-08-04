# Bare-Metal Reliability Platform

A two-node, highly-available reliability platform on real hardware: a floating
VIP with automatic failover, weighted load balancing, app-aware and OS-level
health monitoring, redundant Prometheus, and rolling updates verified to drop
zero requests.

It is a deliberate re-implementation of
[cloud-reliability-platform](https://github.com/ericratz/cloud-reliability-platform)
(FastAPI on AKS, provisioned with Terraform, observed with Grafana + Loki)
without an orchestrator, without a cloud provider, on two second-hand mini-PCs.
The interesting part is not that it works — it is which pieces of the cloud
version turned out to be load-bearing and which were only scaffolding that
existed to satisfy Kubernetes.

---

## Architecture

Both nodes run the identical full stack. keepalived floats a virtual IP between
them; whichever node holds it serves ingress, the other is warm and ready.
There is no dedicated load-balancer node and no single point of failure.

```
                     VIP  192.168.71.245   (floats via VRRP)
                                │
                ┌───────────────┴────────────────┐
        node1  MASTER (prio 110)          node2  BACKUP (prio 100)
     ┌────────────────────────────┐   ┌────────────────────────────┐
     │ keepalived  ◄────── VRRP ───┼───┤ keepalived                 │
     │ nginx :80                   │   │ nginx :80                  │
     │   weighted upstream ─┐      │   │   weighted upstream ─┐     │
     │ FastAPI :8000  ◄─────┴──────┼─┐ │ FastAPI :8000  ◄─────┘     │
     │ Prometheus :9090            │ │ │ Prometheus :9090           │
     │ health-monitor (timer)      │ └─┼──► scrapes both nodes      │
     └────────────────────────────┘   └────────────────────────────┘
        Ubuntu 26.04 · Docker             Rocky 10.2 · Podman
```

Each nginx balances across *both* app instances, so a request landing on either
node's load balancer reaches either backend. Each Prometheus scrapes both nodes
independently — two complete copies of the metrics, the standard Prometheus HA
pattern.

### The hardware is identical — the OS is the variable

| | node1 | node2 |
|---|---|---|
| Machine | GIADA KabyLake mini-PC | GIADA KabyLake mini-PC |
| CPU | Intel i5-7200U (2c/4t, 2.5–3.1GHz) | Intel i5-7200U (2c/4t, 2.5–3.1GHz) |
| RAM | ~4GB (≈3.2GB usable) + 3.9GB swap | ~4GB (≈3.5GB usable) + 3.9GB swap |
| Disk | 2TB Seagate ST2000LV000 (5400rpm HDD) | 2TB Seagate ST2000LV000 (5400rpm HDD) |
| Static IP | 192.168.71.251 | 192.168.71.252 |
| OS | Ubuntu 26.04 (Debian family) | Rocky Linux 10.2 (RHEL family) |
| Runtime | Docker | Podman |

The machines are the same down to the CPU stepping and disk model. That is the
point: because the hardware is controlled for, **any latency difference between
the nodes is attributable to the OS or its configuration** — not to one box
being faster. This is a cleaner experiment than a mismatched fleet could ever
be, and it reframes the whole project around a question a uniform cloud node
pool never forces you to answer: *does one artifact genuinely deploy and behave
identically across a Debian-family and a RHEL-family host?*

Consequences that showed up in practice, not in theory:

- **Two package managers, two firewalls, two MAC layers.** Ubuntu ships ufw +
  AppArmor; Rocky ships firewalld + SELinux **enforcing**. The same container
  needs different host plumbing on each, and SELinux on node2 is a real source
  of "works on node1, silently blocked on node2" bugs. Those steps are
  first-class in [RUNBOOK.md](RUNBOOK.md), not footnotes.
- **Two container runtimes.** Docker on Ubuntu, Podman on Rocky — Podman being
  the RHEL-native, daemonless path. The *same image* runs on both; only the
  thing that starts it differs (Compose vs a systemd Quadlet unit).
- **Spinning disks.** Both nodes run a 5400rpm HDD, so Prometheus's TSDB is on
  rotational storage. Retention is cheap (2TB, nearly empty) but IOPS are
  limited — a real constraint worth naming rather than pretending it is an SSD.

### Load balancing: equal weights, and why that is a result

The nginx upstream uses **equal weights (1:1)**. With identical hardware that is
the correct setting — but it is stated as a *measured* result, not an
assumption: `k6/baseline.js` reports the observed per-node traffic split and
per-node p95, and 1:1 holds. The `weight=` syntax is kept rather than removed so
that (a) the mechanism is already in place if the fleet ever becomes
heterogeneous, and (b) the 1:1 reads as a deliberate finding. "I measured and
they were equal" is a stronger claim than an arbitrary ratio.

---

## What was cut, and why

The honest version of this table is the whole point of the project.

| Cloud version | Here | Why |
|---|---|---|
| **AKS / Kubernetes** | systemd + Docker/Podman + keepalived | At two fixed nodes, K8s solves problems that do not exist: scheduling across a variable pool, service discovery, declarative rollout. What it actually provided — restart on failure, a rollout procedure, health gating, failover — is systemd restart policies, `RUNBOOK.md`, `/health`, and VRRP. The control plane alone would not fit on a ~3GB node. |
| **Terraform / Azure** | none | There is nothing to provision. The infrastructure is two physical machines on a desk. Terraform reproduces cloud resources; reproducing hardware is not a thing it does. |
| **Grafana + Loki** | Prometheus expression browser + journald | Grafana's baseline RAM is not free on a ~3GB box also serving traffic, and at two nodes the queries worth running are ad hoc. Loki → `journalctl`: with two machines, `ssh node2 journalctl -u ...` beats a log pipeline. |
| **Horizontal Pod Autoscaler** | none | Autoscaling needs somewhere to scale *to*. A fixed two-node pool has no elastic capacity; under load the honest answer is "the fleet is at capacity", which the SLO alerts already say. |
| **`kubernetes` Python client in `/health`** | removed | Existed only to read a pod's restart count. Off-cluster it was a hard import that always returned `-1`. Replaced by process `uptime_seconds`, which answers the real question — did this thing actually restart — without an API server. |
| **Cloud load balancer** | nginx + keepalived VIP | The cloud LB was managed and implicitly HA. On bare metal that HA has to be built: two nginx instances and a VRRP-floated VIP with a health-gated failover. This is *added* work the cloud hid, not removed work. |

Deliberately **kept**, because they were never cloud-specific: the FastAPI
service, Prometheus metrics and SLO definitions, failure injection, and the k6
tests. That is the useful finding — the reliability engineering survived the
move intact; only the orchestration scaffolding fell away.

---

## High availability

The load balancer is not a single point of failure, because there is no single
load balancer. Both nodes run nginx and keepalived; a VRRP election keeps the
VIP on exactly one of them, and it moves automatically when that node's nginx
stops serving.

Three design decisions that are easy to get wrong, and were:

- **The health check hits `/nginx-health`, served by nginx itself and never
  proxied.** Not `pgrep nginx` (a wedged nginx still has a process — the check
  would pass exactly when it must fail), and not the proxied `/health` (if both
  app instances die, that fails on *both* nodes and VRRP flaps the VIP between
  two equally-broken load balancers). The VIP should move only when *this
  node's* nginx is the problem.
- **Both keepalived instances are `state BACKUP` with different priorities**
  (110 / 100), not the textbook MASTER/BACKUP pair. `nopreempt` is silently
  ignored on an instance declared MASTER, so the usual pairing gives you
  preemption whether you want it or not — meaning a recovering node yanks the
  VIP back and causes a *second* outage during recovery. Two BACKUPs elect the
  same winner and honor `nopreempt`.
- **`nopreempt` has a price, and `check_nginx` is tracked with no `weight` to
  pay it.** A weighted track script only *adjusts priority*; it never changes
  state. A `nopreempt` peer, by definition, will not take over from a
  lower-priority master — so weight-based tracking and `nopreempt` cancel each
  other out exactly, and the VIP cannot move. Omitting `weight` selects FAULT
  mode, where a failing check makes the node resign with a priority-0 advert,
  which a backup honors regardless of `nopreempt`. This was not reasoned out in
  advance; it was found by §3 Drill A failing on hardware, with 57 dropped
  requests and no VRRP transition on either node. See `CHANGELOG.md`.
- **Failover is measured, not asserted.** `notify.sh` logs every VRRP
  transition with a millisecond timestamp; cross-referenced against
  `scripts/watch-uptime.sh` it yields a real number — "the VIP moved at
  18:42:07.412 and we dropped N requests" — instead of a vague "failover is
  fast." VRRP failover is not instant (~3–4s with these timers), and saying so
  with evidence beats claiming zero.

---

## Endpoints

| Endpoint | Scope | Notes |
|---|---|---|
| `GET /health` | **node-local** | No network calls. Node identity, version, process uptime, memory, disk. |
| `GET /nginx-health` | LB-local | Served by nginx, never proxied. keepalived's failover probe. |
| `GET /slo` | **fleet-wide** | Availability and p95 — from local Prometheus. `null` + `no_data` for an empty window; `status: unavailable` if Prometheus is unreachable. Error rate is `100 - availability_percent`; it is not a separate field. |
| `GET /metrics` | node-local | Prometheus exposition, `brp_` prefix. |
| `GET /` | — | Landing page, names the serving node. |
| `GET /docs` | — | Swagger UI. |
| `GET /reliability/status` | node-local | Current injection state. |
| `POST /reliability/toggle-latency` | node-local | Adds 500ms. |
| `POST /reliability/toggle-errors` | node-local | Forces 500s. |
| `GET /reliability/trigger-error` | node-local | Single 500. |

### Why `/health` does not report SLO

In the cloud version `/health` called Prometheus. Here it does not, and that is
the single most consequential change in the port.

`/health` decides whether a node is safe to put back into the load balancer
pool. Originally it queried Prometheus for SLO data — so a node's health signal
depended on Prometheus being reachable. In this design Prometheus runs on both
nodes and `/health` is local regardless, but the principle stands and is worth
stating: **a liveness check must not depend on a remote system it is meant to be
independent of.** `/health` makes no network calls and answers in ~15ms;
fleet-wide SLO numbers live on `/slo`.

The deploy proved the point harder than intended. `/slo` was configured with
`PROM_URL=http://127.0.0.1:9090` on the reasoning that Prometheus is node-local
— but the app runs in a container and Prometheus runs on the host, so that
address named the container's own loopback and `/slo` could never reach
Prometheus at all. It now uses `host.docker.internal`, which both runtimes
resolve. Each node still queries only its own Prometheus; what was wrong was
the claim that this involved no network hop. Had `/health` still depended on
Prometheus, this would have been a fleet-wide outage of the signal that gates
nodes back into the pool, rather than one degraded endpoint.

A related fix: the original `safe_query` returned `0.0` both when Prometheus was
unreachable and when a query matched nothing, so an unreachable Prometheus
rendered as **`availability_percent: 0.0`** — a healthy system reporting a total
outage. Those cases are now distinct, and unreachable is reported as
unreachable.

That fix went one step short, and `0.2.0` finishes it. Splitting "unreachable"
out left "matched nothing" answering with a per-metric default — `1.0` for
availability — so `/slo` published **`availability_percent: 100.0`** for a fleet
that had served almost nothing, indistinguishable from a measured 100%. The
error had swapped direction, from pessimistic to flattering, which is the worse
of the two: nobody investigates a green number. There are now three outcomes
rather than two, and an empty window reports `null` with the affected metrics
named in a `no_data` array.

---

## Memory budget

Real usable RAM is ~3.2GB (node1) / ~3.5GB (node2) — a Kaby Lake iGPU reserves
the rest of the nominal 4GB. Approximate steady-state on a node holding the VIP
and running everything:

| | approx RSS |
|---|---|
| OS + kernel | 300–450 MB |
| Docker daemon (node1) / Podman (node2, no daemon) | 80–150 MB / ~0 idle |
| FastAPI container | 60–100 MB |
| nginx | ~10 MB |
| keepalived | ~5 MB |
| Prometheus | 100–200 MB |
| **total** | **~0.6–0.9 GB** |

That leaves well over 2GB free, with ~3.9GB swap as a backstop. Memory is
comfortable but not lavish — which is exactly why Grafana, Loki, and a K8s
control plane were not options. This is the constraint that made the cuts
above engineering decisions rather than preferences.

---

## Deploying

Detailed, OS-specific steps — including the ufw / firewalld / SELinux plumbing —
are in [RUNBOOK.md](RUNBOOK.md). In outline:

**Both nodes:** clone the repo, `cp .env.example .env`, set `NODE_NAME`.
`.env` is gitignored and is the only file you *write* per node — the repo also
ships per-node variants you pick between rather than edit (`keepalived-nodeN.conf`,
`health-monitor-nodeN.conf`).

**node1 (Ubuntu / Docker):**
```bash
docker compose up -d --build
```

**node2 (Rocky / Podman):** build the same image, run it under a systemd
Quadlet unit (Podman is daemonless, so `restart: unless-stopped` alone does not
survive reboot — the Quadlet unit is what makes the container systemd-managed).

**Both nodes** additionally run nginx, Prometheus, and keepalived from the
configs in `deploy/`.

---

## Verifying the zero-downtime claim

The claim is only worth anything with evidence attached, so the tooling produces
some. All traffic goes at the **VIP**, not a node IP — and therefore **from
node2**, since the VIP is only reachable from the lab LAN and node2 is the node
that is neither the balancer nor the VRRP master.

```bash
# on node2:
# terminal A — every request, timestamped, with the node that served it
./scripts/watch-uptime.sh http://192.168.71.245/health

# terminal B — 20 req/s, zero-failure threshold
k6 run -e BASE_URL=http://192.168.71.245 -e MAX_VUS=50 -e DURATION=15m k6/rolling-update.js

# terminal C — perform RUNBOOK.md
```

Generating load from a node that is also serving it is a compromise, and worth
naming rather than burying: there is no third machine on the LAN. It costs the
per-node latency figure, which now includes k6's own consumption on node2. It
does not cost the zero-dropped-requests result, which counts failed responses
and is indifferent to which host asked.

`watch-uptime.sh` exits non-zero if any request failed, so it cannot report
success by default. Both it and the k6 scenario were tested against a
deliberately injected outage, to confirm they *detect* failure rather than
reporting clean runs unconditionally.

The k6 scenario uses `constant-arrival-rate` rather than a fixed VU count on
purpose: with VUs, requests queue behind slow responses and offered load
silently drops exactly when a node goes away — the test stops exercising the
window it exists to measure. An open model keeps sending traffic regardless,
like a real client.

---

## Layout

```
app/                     FastAPI service (ported, cloud deps stripped)
deploy/nginx/            LB config; upstream split out so a deploy edits one file
deploy/prometheus/       scrape config + SLO recording and alerting rules
deploy/keepalived/       VRRP config for both nodes + health-check / notify scripts
deploy/podman/           node2 Quadlet unit — what makes the container reboot-safe
deploy/systemd/          health-monitor drop-ins; doubles as the registration checklist
k6/baseline.js           normal-condition load; reports traffic split by node
k6/rolling-update.js     zero-failure-threshold load for the rollout window
scripts/pool.sh          add/remove a node from the nginx pool, syntax-checked
scripts/watch-uptime.sh  per-request evidence log
RUNBOOK.md               rolling-update and failover procedures
```

---

## Status

Verified locally: app port, container build, Prometheus config + rules
(`promtool`-validated), `pool.sh` (15 tests), `watch-uptime.sh` (7 tests incl.
injected-outage detection). Both nodes provisioned with static IPs and their
container runtimes.

**`RUNBOOK.md` §1 is complete — the whole platform is running on real
hardware.** Firewalls configured per OS (§1a); linux-health-monitor v3.2 on a
timer on both nodes (§1·1); the app tier serving `0.1.0` on both (§1b), Docker
Compose on node1 and a rootless Podman Quadlet unit on node2; nginx on both
(§1c), each balancing across both backends, with the monitor's HTML report at
`/report`; redundant Prometheus (§1d), 3.13.2 from the same upstream tarball on
both nodes, each scraping both, four targets up from each instance, `/slo`
answering; and keepalived floating the VIP (§1e).

`192.168.71.245` is live on node1 with node2 as a healthy backup, verified as
holder rather than assumed: from node2, ARP for the VIP resolves to
`24:1c:04:14:42:ce` — node1's MAC — which is the same `answered_by` signal the
health monitor now checks every cycle. A request to the VIP traverses the whole
chain, and returned `node2`, confirming that node1's nginx balances into node2's
backend.

**`RUNBOOK.md` §2 is executed and measured: zero dropped requests across a
two-node rolling update.** 24,001 requests over 20 minutes at 20 req/s through
the VIP, `http_req_failed rate==0`, k6 exit `0`, alongside a second independent
watcher sampling every 200ms.

The rollout is proven from the access log rather than inferred from totals.
node2 served **zero** requests from 23:30:25 to 23:31:53 while it was drained
and rebuilt, then node1 served zero from 23:33:57 to 23:35:32 — visible as
per-minute counts of 309 and 82 against a steady ~738, and as node2 carrying
the entire fleet at ~1500/min while node1 was out. Both nodes moved `0.2.1` →
`0.2.2`, each verified directly while out of the pool, each showing a small
`uptime_seconds` to prove a real restart rather than a lingering container.

The final split was 11947/12054 — near-even, and that is the *expected* result,
not a sign nothing happened: a full rolling update drains both nodes for
comparable periods, so the two shortfalls cancel. Totals cannot demonstrate a
rollout. Only timestamps can, which is why the access-log breakdown is the
evidence and k6's number is the claim it supports.

**Both nodes run the monitor and exit 0** with every component registered. That
number is the deploy's actual output: the registration discipline means a clean
exit says "this node is healthy as configured" rather than "nothing is being
watched."

**The 1:1 upstream split is measured, not assumed.** Under k6 through the VIP:
**300/301** over 601 requests, and **182/186** on a second run — 49.5/50.5.
(An earlier 40-request curl sample gave 19/21, which is the same answer with
far less confidence.) Short samples do *not* alternate cleanly — nginx keeps
round-robin state per worker process, and there are four workers per node —
which is why the split is read from volume, not from a handful of requests.

**Per-node p95, however, is not comparable between these nodes.** Loading each
backend directly gives 30.8ms on node1 against 5.1ms on node2, while the two
apps answer in 1.9ms and 1.85ms *measured on their own hosts*. Identical image,
identical code, equally fast in place — the difference appears only in requests
arriving from another machine, and it is variance rather than a constant offset,
which is why p95 shows it and means do not. The unexamined path is node1's
ingress chain (ufw → conntrack → DNAT → Docker bridge), which node2's pasta
forwarding has no equivalent of. See `CHANGELOG.md`.

That exit code is the point of the registration discipline: `HEALTH_SERVICES`
and `HEALTH_APP_ENDPOINTS` start trimmed to what exists and grow as each
component lands, so the monitor is meaningful during the deploy rather than
sitting at a standing WARNING until the platform is finished.

§1b and §1c produced the project's sharpest portability findings so far, and
every one of them was silent. Podman discarded the Dockerfile `HEALTHCHECK`
under the OCI image format; pasta's wildcard bind reset IPv6 connections that
Docker's userland proxy quietly bridges; the Quadlet's `EnvironmentFile`
shadowed the version baked in by the build-arg. Then nginx: the two distros
ship their conflicting default server block in places that admit no common fix,
Debian's package start left `systemctl is-active`, `nginx -t` *and* `nginx -T`
all reporting success while the wrong config served traffic, and node2 needed
SELinux work node1 has no equivalent of.

The recurring shape is worth stating plainly, because it is the answer to the
question this project was built to ask: the divergences are not where the two
OSes are *spelled* differently — those are easy and the runbook always had them
— they are where one OS silently does something for you that the other does
not. Details in `CHANGELOG.md`.

Pending:

- [ ] VIP failover drills (§3) executed and measured on real hardware — §2 is
      done; these are the remaining numbers this project exists to produce
- [ ] SLO numbers are not yet real. `/slo` no longer *fabricates* them — an
      empty window now reports `null` and says which metrics it has no data for,
      instead of the `availability_percent: 100.0` it published throughout §1
      — but null is an honest absence, not a measurement. k6 in §2 is what
      makes them mean something
- [ ] Per-node p95 from the §2 run is not a clean number, for two separate
      reasons: k6 ran on node2, which is also one of the two backends under
      test, and node1 is independently slower to answer off-box requests (see
      above). The zero-dropped-requests result is unaffected — it counts failed
      responses, and does not care which host asked
- [ ] node1's ingress-path latency is diagnosed only by elimination. The app,
      the image, Docker's port publishing and the link are all ruled out by
      measurement; ufw/conntrack/DNAT/bridge is the remaining candidate and has
      not been instrumented
- [ ] node2's app logs are not reachable via `journalctl --user`, so the
      monitor's journal check has nothing to read there; `podman logs` works
- [ ] `httpd_can_network_connect` was set on node2 pre-emptively and is not
      proven to have been required — unlike the `/var/www/health` relabel,
      which `restorecon` confirmed
- [ ] Prometheus is installed from a tarball and gets no distro security
      updates on either node; upgrades are manual and unmonitored

Deferred: Alertmanager routing (rules evaluate and display in Prometheus's UI,
they just do not notify anywhere yet).

---

## Related

- [linux-health-monitor](https://github.com/ericratz/linux-health-monitor) —
  the OS-level agent, kept as its own repo and consumed here as a deployed agent
  rather than vendored in
- [cloud-reliability-platform](https://github.com/ericratz/cloud-reliability-platform) —
  the AKS/Terraform/Grafana original this is a translation of
