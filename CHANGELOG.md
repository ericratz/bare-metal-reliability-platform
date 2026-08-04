# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **The VIP could not fail over at all, and §3 Drill A is what found it.**
  `check_nginx` tracked with `weight -20` on both nodes. The reasoning in the
  config was that a failing check drops node1 from 110 to 90, below node2's
  100, "so the VIP moves." It does not. Per `keepalived.conf(5)`, a non-zero
  weight *adjusts priority and nothing else* — only weight 0 (the default)
  transitions the instance to FAULT after `fall` failures. So node1 stayed
  MASTER at priority 90 and kept advertising, and node2 declined to preempt a
  lower-priority master because it carries `nopreempt`, which is the one flag
  whose entire purpose is to decline exactly that. Nothing resigned, so nothing
  moved. Both halves were individually correct and documented; the defect lived
  in the interaction.

  Fixed by dropping `weight` from the tracking block on both nodes. A FAULT
  resignation sends a priority-0 advert, and a backup receiving priority 0
  promotes itself immediately — that path is evaluated *before* the nopreempt
  guard, so it is unaffected by it. `nopreempt` keeps doing its real job:
  stopping a recovered node1 from snatching the VIP back and causing a second
  outage.

  **Measured, 2026-08-04 01:25:12Z.** `systemctl stop nginx` on node1 (the VIP
  holder) produced **57 consecutive dropped requests** at the VIP, an unbroken
  run of `code=000`, **zero VRRP transitions logged on either node**, and no
  recovery whatsoever until nginx was restarted by hand. Confirmed after the
  fact by the check in §3: `.251` reported `1` and `.252` reported `0`, so the
  VIP was still on node1 — and `nopreempt` means it cannot have drifted back,
  so it never left.

  The `nginx.service` journal accounts for the window with nothing left over:

  | | |
  |---|---|
  | `01:25:12.234` | systemd begins stopping nginx |
  | `01:25:12.242` | first dropped request — **8ms later** |
  | `01:25:24.171` | operator starts nginx by hand |
  | `01:25:24.182` | last dropped request |
  | `01:25:24.213` | nginx `Started` (42ms to come up) |

  So the ~12s is exactly stop-to-start: *the operator's reaction time, not a
  failover time.* It is quoted nowhere as a metric — the true figure without
  intervention is unbounded. A side benefit: the watcher's timestamps track
  systemd's journal to within tens of milliseconds in both directions, which is
  independent evidence that `watch-uptime.sh` measures what it claims to.

  Three things worth keeping from this:

  - **The health check was never wrong.** `check_nginx.sh` detected the
    stopped nginx correctly, on schedule, on the right endpoint — its own
    header warns at length against checks that pass when they should fail. It
    passed that bar and the VIP still did not move. A health check is only as
    good as the mechanism it is wired into, and that mechanism had never been
    exercised.
  - **A slow failover and no failover look identical from the watcher.** Both
    are a run of `000` at the VIP. Only the `keepalived-notify` tails
    distinguish them, which is why §3 now treats tailing *both* nodes as part
    of the procedure rather than a convenience.
  - **This is the third instrument-versus-reality gap in the same project**,
    after k6 reporting a rollout that never happened and `watch-uptime.sh`
    inventing an outage that never happened. Each was found only by running the
    thing on hardware. Config that has been read carefully and never executed
    is not evidence.

- **`notify.sh` logged a different format on each node, and the platform gave
  no sign of it.** This is the file the zero-downtime claim rests on — it
  timestamps every VRRP transition so a failover can be correlated against the
  per-request log and turned into a real number. Both defects were found by
  reading the two nodes' output side by side after §1e brought keepalived up,
  not by anything failing:

  - **`host=` was empty on node2.** `$(hostname)` returned nothing because
    SELinux denies `keepalived_t` even `getattr` on `hostname_exec_t` — the
    binary is installed and works from a normal shell, but not from a child of
    keepalived. node1 is unconfined and had no such problem. Now uses the
    `$HOSTNAME` bash builtin, which needs no exec. The failure mode is the
    quiet kind: the line still logged and still parsed, it merely dropped the
    field saying which node emitted it.
  - **The timestamps had different precision.** The script asked for `%3N`
    (milliseconds); Rocky's GNU `date` honoured it and emitted three digits,
    while Ubuntu 26.04 ships the Rust coreutils, whose `date` ignores the width
    modifier and emitted nine. Now takes `%N` and truncates in bash, which is
    byte-identical under both. Same family as `tail -5` being rejected on
    node1 — Ubuntu's coreutils replacement is a standing source of these.

  Neither would have surfaced until someone tried to correlate a failover
  across both nodes during a drill, which is exactly when the instrument needs
  to be trustworthy.
- **Documented the two SELinux AVCs keepalived raises on node2**, and left the
  benign one alone. The `setattr` denials on `check_nginx.sh` and `notify.sh`
  come from `install` labelling them `etc_t`; keepalived is refused and carries
  on regardless — `VRRP_Script(check_nginx) succeeded` appears in the same log.
  Silencing it would mean shipping a local policy module to suppress a denial
  that breaks nothing, which trades a harmless log line for custom policy
  nobody will remember writing. `RUNBOOK.md` §1e now says which denial matters
  and which does not, since the audit log shows both together.

- **`PROM_URL=http://127.0.0.1:9090` could never have worked.** The app runs in
  a container and Prometheus runs on the host, so that address names the
  container's own loopback, where nothing listens. `/slo` reported
  `status: unavailable` on both nodes the moment Prometheus existed — correctly,
  since the app genuinely could not reach it. Wrong since §1b; invisible until
  §1d, because until then there was no Prometheus to fail to reach.

  This also corrects the 0.1.0 entry below and the README, both of which said
  the move to `127.0.0.1:9090` meant `/slo` "never crosses the network." The
  *intent* holds — each node queries only its own Prometheus, never the peer's
  — but the mechanism described does not exist under a container runtime. Left
  as written below; this is the correction.

  Now `http://host.docker.internal:9090`, which resolves to the host under both
  runtimes: Podman aliases it natively alongside `host.containers.internal`,
  and `docker-compose.yml` maps it via `extra_hosts: host-gateway`. One
  identical value on both nodes, which is the reason for preferring a name over
  either node's address.
- **node1 additionally needed a ufw rule, and the Compose network subnet is now
  pinned to make that rule durable.** §1a opens `9090` to `192.168.68.0/22`, but
  a container's source address is on the Docker bridge, so container-to-host
  `:9090` was dropped. The symptom was a *timeout* rather than a refusal, which
  reads as a hung Prometheus rather than a blocked packet — node1's loopback
  test refused immediately while the gateway test hung, and that difference is
  what identified it.

  `docker-compose.yml` now pins `172.28.0.0/24` instead of accepting Docker's
  address pool, because a firewall rule naming a subnet Docker chose at random
  silently stops matching the first time the network is recreated. Compose had
  assigned `172.18.0.0/16`; that was luck, not a contract.

  node2 needed neither change: pasta routes the container to the host directly
  and firewalld never sees the traffic. Same logical connection, working out of
  the box on one node and requiring pinned addressing plus an explicit firewall
  rule on the other.

- **`RUNBOOK.md` §1c assumed nginx was installed, that the distro ships no
  conflicting server block, and that `systemctl enable --now` starts what you
  configured.** All three were wrong, and in different ways per OS:

  - nginx is on neither node. node1's image is minimized.
  - Both packages ship a `listen 80 default_server` block, so `nginx -t` fails
    with `duplicate default server` — but Debian puts it in
    `sites-enabled/default` (unlink it) and RHEL puts it *inside*
    `/etc/nginx/nginx.conf`, where there is no file to unlink. Same conflict,
    no portable fix.
  - Debian starts a service on package install, so nginx was already running
    before the config existed and `--now` did nothing to an active unit. RHEL
    does not auto-start, so node2 was never exposed. §1c now issues an explicit
    `restart`, which is correct on both.

  The `enable --now` case is the one worth keeping: `systemctl is-active` said
  `active`, `nginx -t` said the config was valid, and `nginx -T` printed the
  new config in full — while the stock default site served 404 on
  `/nginx-health`. `nginx -T` reads from disk, not from the running master, so
  it *cannot* detect this and reads as confirmation. `systemctl reload` did not
  fix it either; only a full restart did. The only check that caught it was
  `curl /nginx-health`, which is why that step is a verification and not a
  formality. Recorded as observed — the package's install-time
  `Upgrading binary nginx` (a USR2 binary upgrade) is the suspected cause, not
  a confirmed one.
- **node2 needed two SELinux changes to serve `/report` and proxy at all.**
  `/var/www` is not RHEL's web root, so `/var/www/health` was created `var_t`
  and nginx could not read the report — confirmed by `restorecon` relabelling
  it to `httpd_sys_content_t`. `httpd_can_network_connect` is also set, since
  SELinux otherwise blocks nginx from making outbound connections and every
  `proxy_pass` to `:8000` fails; it was set pre-emptively, so unlike the
  relabel it is not *proven* to have been required here. node1 needs neither —
  AppArmor's nginx profile permits both.

  The proxy case is the dangerous one and is why it is set anyway: with it
  off, `/nginx-health` still returns 200 because nginx serves it locally, so
  keepalived would see a perfectly healthy load balancer in front of a proxy
  that cannot reach a single backend. §1e's health check is designed to answer
  "is *this* node's nginx serving", and this is the one failure where that
  correct design still points at a broken node.
- `deploy/nginx/brp.conf` told you to create `/var/www/health` with
  `install -d -m 755`, contradicting §1c's `2750` and the setgid reasoning two
  files away. Following the comment would have published the report — which
  enumerates listening ports, unit names, containers, addressing and MACs — to
  every local account, and nothing would have looked wrong, since the page
  renders identically either way. Corrected to the per-node setgid form.

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

  Fixed by passing `--ipv4-only` through to pasta
  (`Network=pasta:--ipv4-only` in `deploy/podman/brp-api.container`), so `::1`
  refuses cleanly and clients fall back; the verification commands address
  `127.0.0.1` explicitly rather than relying on that fallback. Nothing in the
  platform needed v6 ingress — the nginx upstream, the Prometheus targets,
  `PROM_URL` and the health monitor's endpoints are all v4 literals, which is
  why this stayed invisible.

  **`PublishPort=0.0.0.0:8000:8000` does not work and was the first attempt.**
  The bind address lands in the unit and is then discarded: podman hands pasta
  a bare `-t 8000-8000:8000-8000`, the wildcard bind survives, and `::1` keeps
  resetting. Nothing reports the setting as ignored — `podman ps` prints
  `0.0.0.0:8000->8000/tcp` in both cases, so the only ground truth is the pasta
  command line in `pgrep -a pasta` and whether `ss` shows `0.0.0.0:8000` or
  `*:8000`. A config knob that is accepted, displayed as applied, and silently
  inert is worth recording as its own finding.

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

- **`deploy/prometheus/prometheus.service` — Prometheus installed from the
  upstream tarball on both nodes rather than from a package.** Ubuntu carries
  `prometheus` in universe (2.53.x); Rocky 10 carries it in no enabled repo at
  all, and EPEL dropped it after EL8. Package-where-available would have put a
  2.x collector on node1 and a 3.x on node2, with different unit files, service
  users and data paths.

  That is specifically disqualifying here. The hardware is identical so that
  any per-node difference in the metrics is attributable to the OS; two
  different collector versions would hand every such difference a second
  candidate explanation and quietly void the measurement the whole two-node
  setup exists to make. Both nodes now run 3.13.2 from the same tarball, the
  same shipped unit, and the same config — `external_labels.replica` is the
  only line that differs.

  The cost is stated rather than hidden: this binary gets no distro security
  updates, and upgrading means repeating the install on both nodes by hand.
- **`sudo promtool` works on node1 and not on node2.** RHEL's sudoers
  `secure_path` is `/sbin:/bin:/usr/sbin:/usr/bin` and excludes
  `/usr/local/bin`; Debian's includes it. The binary is installed identically
  and is on the interactive `PATH` on both nodes — it is invisible only
  *through sudo*, and the error names the command rather than the cause, so it
  reads as a failed install. `prometheus.service` is unaffected because
  `ExecStart` is absolute. §1d now validates without `sudo`, which it never
  needed: the config is world-readable.
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

- **§2 and §3 now say where to run from, and k6 has an install procedure.**
  Both sections drive traffic at the VIP, and neither said where to run from,
  which would have stopped the first measured run at its first command.

  **The reason first given here was wrong** — "the workstation is NAT'd off the
  lab LAN" — and is superseded by the workstation-network entry below: it does
  reach the VIP. The conclusion (run from node2) survives on the replacement
  reason, the workstation↔node1 stalls. RUNBOOK §1c, §1e, §2 step 0 and §3
  carried the false reason until 2026-08-03.

  **k6 runs on node2**, capped at `MAX_VUS=50`. It is not packaged for Rocky 10;
  installed as a static binary from the upstream release, with the same
  no-distro-security-updates caveat as Prometheus in §1d, accepted for the same
  kind of reason — an operator tool run by hand, listening on nothing.

  The honest part: **node2 is also one of the two backends under test.** There
  is no third machine on the LAN, so this is a constraint, not a preference.
  node2 is the less bad end of it, because node1 holds the VIP *and* is the
  nginx every request traverses, which would put client, balancer, VRRP master
  and backend on one 2-core box. What it costs is per-node `p95_latency_ms`,
  which now includes k6's own consumption and must not be quoted as a clean
  figure. What it does not cost is the zero-dropped-requests result, which
  counts failed responses and is indifferent to which host asked. `maxVUs` is
  capped because an open-model executor allocates VUs to hold the request rate
  steady, so an unbounded ceiling turns a latency blip into a self-inflicted
  load spike that the test then reports as the platform's fault.

  Also: §2 step 7 said only "same sequence" for node1, while node1 is Compose
  and node2 is a Quadlet — the node1 update command existed nowhere. Written
  out. And the k6 summary writes to `evidence/`, which k6 will not create, so
  step 0 makes the directory first.

- **§2's drain check counted the operator's own traffic as proof the drain had
  failed.** Steps 3 and 6 ran
  `tail -20 /var/log/nginx/brp_access.log | grep -c '192.168.71.252'`. The log
  line is `$remote_addr [...] upstream=$upstream_addr [...]`, so that address
  matches both when node2 **served** a request and when node2 **sent** one —
  and node2 is exactly where k6 and several of the runbook's own `curl`
  commands run. Found in the rehearsal: a correctly drained node2 returned
  `15`, from client-address lines left by the evening's load tests. The
  `brp.conf` comment already said `upstream=$upstream_addr` "is the important
  field: without it the access log cannot prove traffic actually shifted" — the
  format was designed for this and the check ignored it. Now greps
  `upstream=192.168.71.252`.

  Step 6 had the same bug pointed the other way and it was the more dangerous
  of the two: it passes on a **non-zero** count, so client-address lines would
  have satisfied it while node2 was still out of the pool — a green light to
  proceed, on the step that returns a node to service.

  Both steps also relied on `tail -20`, which is a line count and not a time
  window. On a quiet fleet those lines can predate the drain entirely, so a `0`
  meant "nothing happened recently", not "node2 is receiving nothing" — absence
  of evidence, standing in for the check you actually want before restarting a
  service. Both steps now send 20 requests through the VIP first and read only
  the window that traffic produced, turning each into a positive proof.

- **§2 never said to `git pull` before building, and the omission was invisible
  by construction.** `APP_VERSION` comes from `.env` and is set independently of
  the checkout, so building a stale tree yields an image tagged `0.2.0`, whose
  `/health` reports `version: 0.2.0`, containing the previous code. Step 5 gates
  on exactly that version string and would have passed it — as would every other
  signal, since the container really did restart and `uptime_seconds` really is
  small. The result is a green rollout of nothing, followed by debugging a
  missing fix on the node that just reported deploying it.

  Now a precondition that pulls both nodes and compares `rev-parse --short HEAD`
  between them, plus an explicit `git pull` in steps 4 and 7. Stated there
  because it is the kind of step that reads as obvious and gets skipped: node2
  had been pulled repeatedly during the k6 work while node1 sat several commits
  behind, and nothing in the procedure would have said so.

### Fixed

*This is what `0.2.0` carries.* §2 proves a rolling update by showing `version`
change per node, so it needs a version with something in it; the honest
candidate was the defect §1d exposed and the README has been carrying as a
pending item ever since.

`0.2.0` was deployed to node2 alone, out of the pool, with no load running —
a rehearsal, not the measured run. It verified against a live Prometheus with
node1 still on `0.1.0` beside it as a control: `jq` parsed both responses,
node2 returned a computed `availability_percent` with no `no_data` key while
node1 returned the same figure from a default it had never computed, and
`error_rate_percent` was present on one and absent on the other. The two
nodes' `p95_latency_ms` differed by 4% (8.71 vs 8.40) — separate Prometheus
instances scraping the same targets at different offsets, which is the
disagreement the `/slo` docstring predicts and the size that means noise
rather than a failing scrape.

**`0.2.1` is a version bump with no code change, and that is deliberate.** The
measured §2 run tests the rollout mechanism, not a payload; a no-op bump
isolates that variable, so a dropped request can only be the procedure. It is
also the only way both nodes' versions move in one run, node2 already being on
`0.2.0` from the rehearsal.

- **`/slo` reported `availability_percent: 100.0` for a fleet that had served
  almost nothing.** `safe_query()` took a per-metric `default` and returned it
  whenever Prometheus answered with an empty result, and `get_availability()`
  passed `default=1.0` on the reasoning that no series yet means nothing has
  failed yet. True, and still the wrong thing to publish: the response could
  not distinguish a measured 100% from an idle window, and what it chose to
  show was the flattering reading. A fabricated SLO is worse than an absent
  one — an absent one prompts a question, a fabricated one reads as evidence,
  and this one sat on the dashboard for the whole of §1.

  `safe_query()` now returns a third value, `NO_DATA`, distinct from both a
  float and the `None` that means Prometheus is unreachable. `/slo` renders it
  as an explicit `null` and names the affected metrics in a `no_data` array, so
  "no traffic in this window" is something the response states rather than
  something you have to know to infer.

- **The empty-result path was reached during healthy traffic, not just idle
  traffic** — which is why the wrong default went unnoticed for so long.
  `sum(rate(brp_requests_total{status=~"5.."}[2m]))` over a selector matching
  nothing yields an empty vector rather than a zero, and empty propagates
  through the whole expression, so *any* window with no 5xx returned nothing at
  all. `default=1.0` was silently standing in for the healthy case as well as
  the idle one, and the two were about to become indistinguishable in exactly
  the run meant to measure them. The 5xx term is now `… or vector(0)`, leaving
  one thing that can empty the expression: no requests at all.

- **A quiet window produced invalid JSON, not just a misleading number.**
  `histogram_quantile()` over a histogram with no observations returns NaN;
  `float('nan')` passed straight through to the encoder, which emits a bare
  `NaN` token. That is not JSON — a strict parser rejects the entire document,
  not the one field, so `curl … | jq` on `/slo` failed outright rather than
  showing a null p95. Non-finite values are now folded into `NO_DATA` at the
  query boundary, where the string forms Prometheus actually sends (`"NaN"`,
  `"+Inf"`) are still visible; `float()` accepts all of them without complaint,
  so the check has to happen after the conversion.

- **`/slo`'s own docstring was the last copy of the corrected claim.** The
  `PROM_URL` entry above says it corrects "the 0.1.0 entry below and the
  README" — it missed the code. The docstring still said `/slo` queries node1's
  Prometheus so both instances return the same numbers, which describes a
  design that was never built. Now states what actually happens: the numbers
  are fleet-wide because Prometheus sums across both nodes' targets, while the
  query stays node-local so no single instance's loss takes `/slo` down
  everywhere. Worth more than a typo fix — a docstring is where the next person
  looks before the changelog, and this one contradicted the deployment.

- **`scripts/watch-uptime.sh` was committed non-executable**, while `pool.sh`
  was not. §2 step 0 and all of §3 invoke it as `./scripts/watch-uptime.sh`, so
  the first command of the first evidence-gathering step would have failed with
  a permission error on both nodes. Local-only artifact of how the file was
  created; invisible on the workstation because it had never been run from a
  fresh clone.

- **Both k6 scripts reported per-node traffic by scraping a metric key format
  that k6 v2 does not produce.** Found on the first real run of either script —
  they had never been executed anywhere, which is why this survived to now.
  `Counter.add(1, {node})` records the tag on each sample, but the tag-split
  sub-metric only reaches `handleSummary` if k6 materialises it, and the key it
  arrives under (`node_hits{node:node1}`) is an output detail, not an API.

  The two failures were different, and the second is the worse one:

  - `rolling-update.js` printed `total requests : 601`, `failed : 0`, and
    `ZERO DROPPED REQUESTS` above an **empty `served by:` section**. Half of
    §2 step 8's check vanished under a headline that said the run passed.
  - `baseline.js` would have printed `no /health responses carried a node
    field` — not a missing result but a *wrong diagnosis*, blaming the app for
    omitting a field it had faithfully returned on every request. Since the
    per-node split is the only thing that script exists to produce, it would
    have sent someone to debug the wrong component entirely.

  Both now use one metric per node (`node_hits_node1`, `node_latency_node1`, …)
  instead of one metric tagged by node. Explicit metrics are always present in
  `data.metrics`, so the summary cannot silently lose a dimension when an
  output format changes. A `node_hits_unknown` counter catches a `node` value
  that is neither — that means `NODE_NAME` is wrong somewhere, which
  misattributes every per-node number and previously looked identical to a node
  serving no traffic. `rolling-update.js` also says so explicitly when nothing
  could be attributed, rather than printing an empty list: a blank section
  reads as "nothing to report" when it means "the reporting broke."

  `baseline.js` now also prints per-node p95, which its own closing advice
  ("if p95 differs sharply between nodes…") has always told the reader to
  compare while never actually showing it — the Trend was tagged by node and
  therefore just as unreachable.

- **`watch-uptime.sh` fabricated an outage every time you stopped it.**
  `trap 'running=0' INT TERM` sets the loop flag, but SIGINT goes to the entire
  foreground process group — so Ctrl-C also kills the in-flight `curl`, `$meta`
  comes back empty, and the `[[ -z "$code" ]] && code="000"` guard scores that
  as a failed request. Always the final sample, and enough to flip a clean run
  to `Zero-downtime NOT demonstrated` with a non-zero exit.

  Intermittent, which is why it survived: `curl` occupies roughly 10ms of each
  200ms cycle, so an interrupt lands mid-request about one stop in twenty. An
  earlier 5031-request run this same evening stopped cleanly. It bit the §2
  run — one reported failure at 23:37:57, two minutes after the rollout had
  finished, with nginx's access and error logs both silent for that second and
  k6 running continuously through it. The loop now breaks on an interrupted
  sample rather than scoring it.

  Worth stating alongside the k6 defect above, because they are mirror images:
  k6 printed `ZERO DROPPED REQUESTS` for a rollout that never happened, and
  this invented a drop that never happened. Both instruments were "tested."
  Neither had been run against a real rolling update on hardware, which is the
  only thing that exercises the paths where they differ from their own claims.

  **Verified 2026-08-03, with a negative control.** A ~1-in-20 bug cannot be
  confirmed fixed by stopping the script once, so the window was made
  deterministic instead: a local server delaying `/health` by 2s against the
  default 0.2s interval puts `curl` in flight for ~91% of each cycle. Against a
  copy with the guard line deleted, every mid-`curl` interrupt reproduced the
  defect — `failed=1`, exit 1, final sample `000`. Against the shipped script,
  interrupts at 3.0s, 5.2s and 7.4s all gave `failed=0`, exit 0, with the
  interrupted sample correctly absent from the log rather than scored. The
  inter-request sleep window (50ms `curl`, 3s interval) is clean on both, which
  places the defect precisely in the `curl` window and confirms the guard does
  not disturb the path that already worked. Then live: 30s at the VIP, **117/117
  succeeded**, exit 0, attribution node2 60 / node1 57.

  One trap for anyone re-running this: reproducing a terminal Ctrl-C requires
  `kill -INT` to the *process group*, and the harness must enable job control
  (`set -m`). Bash starts background jobs with SIGINT set to `SIG_IGN` when job
  control is off, and a child can never re-trap a signal ignored at exec — the
  first attempt sent SIGINTs that were silently discarded and the script ran on
  forever. A harness with that flaw passes the broken script too.

- **k6's summary briefly claimed it could detect whether a rollout occurred,
  and it cannot.** A check was added warning when the two nodes served within
  10% of each other, reasoning that draining a node starves it of ~1200
  requests per minute. True for one node — but §2 drains *both*, for roughly
  equal periods, so the shortfalls cancel. A run where nothing was drained
  measured 9010/8990; a correct rollout measured within a few hundred of even
  as well. No threshold separates them, so the check fired on correct runs and
  stayed silent on the failure it was written for. Removed.

  Whether traffic left a node is a question about *time*, and the summary only
  has totals. The access log has both, and the per-minute breakdown is now
  printed by the summary as the thing to actually go and check.

`status` deliberately stays `"ok"` when Prometheus answers but every metric is
null. It reports whether the endpoint could do its job, not whether traffic
happened to be flowing — the health monitor gates on that field, and a quiet
night is not a fault. Changing it would have turned an idle fleet into a failed
monitor run, which is the opposite of the point.

Left alone knowingly: `clamp_min(…, 1)` on the denominator still inflates the
request rate below 1 req/s and understates the error rate there. §2 runs at
20 req/s, well clear of the floor. Noted in the code rather than fixed, because
the fix is a judgement about what an error rate means at near-zero traffic and
that deserves its own change.

### Measured

- **§2 executed on hardware: zero dropped requests across a two-node rolling
  update.** 24,001 requests over 20 minutes at 20 req/s through the VIP,
  `http_req_failed rate==0`, k6 exit `0`, with `watch-uptime.sh` sampling every
  200ms in parallel. Both nodes went `0.2.1` → `0.2.2`, each drained, rebuilt,
  verified directly, and returned to the pool before the other was touched.

  Evidence is the access log, not the totals. Per-minute counts of requests
  node2 served: ~738 steady, then **309** and **82** across its drain window
  (23:30:25–23:31:53), then back to 738. And **1498 / 1152** while node1 was
  out (23:33:57–23:35:32) — node2 carrying the whole fleet at roughly double
  its share. The partial-minute figures match the drain timestamps to within a
  few requests, which is what makes them evidence rather than a plausible
  shape.

  Final split 11947/12054. Near-even is the **expected** result: both nodes are
  drained for comparable periods, so the shortfalls cancel. That is why the
  removed heuristic could not work, and why a timestamped source is the only
  thing that can answer the question.

  The one reported failure came from the watcher's own Ctrl-C (see above) two
  minutes after the rollout completed, with nginx's access and error logs
  silent for that second and k6 running continuously through it.

### Removed

- **`error_rate_percent` from `/slo`, and `get_error_rate()` with it.** The two
  queries were complements over identical terms — `1 - x` and `x` — so the
  field was always exactly `100 - availability_percent`, and producing it cost
  a second Prometheus round trip on an endpoint the health monitor polls every
  cycle and k6 hits under load. `/slo` now makes two queries per request
  instead of three.

  The redundancy was not merely wasteful. Rounded independently, the two fields
  could land on 99.99 and 0.02 in the same response, which invites reading
  agreement between them as corroboration — two fields that look like separate
  measurements confirming each other, when one is arithmetic performed on the
  other. Derive it at the point of use.

  Nothing outside the app consumed it. Prometheus computes its own
  `brp:error_rate:ratio5m` recording rule in `deploy/prometheus/rules/`, which
  is what the alerts fire on and is untouched by this. The removal is a
  response-shape change, so it belongs to `0.2.0` alongside the fixes above
  rather than arriving unannounced later.

### Known gaps

- **node1 answers external requests measurably slower than node2, and only
  from off-box.** Found while installing k6, before §2 ran — the first time
  either node had been load-tested on hardware. Measured, in order, because
  each result invalidated the previous explanation:

  | measurement | node1 | node2 |
  |---|---|---|
  | app from its own host | 1.9ms (published) / 1.7ms (container IP) | 1.85ms |
  | single request from the *other* node | 2.87 / 5.65 / 8.06ms | 2.42 / 2.45 / 3.85ms |
  | p95 under 10 VUs, direct to backend | **30.8ms** | **5.1ms** |
  | p95 under 10 VUs, via the VIP | 13.0ms | 5.4ms |

  **The two apps are equally fast.** Measured on their own hosts they are
  within 0.05ms of each other, so this is not the image, the interpreter, or
  the code — all of which are identical artifacts. Docker's port publishing is
  not the cost either: via the published port and straight to the container IP
  differ by 0.3ms on node1.

  **The link is not the cause.** 60 ICMP packets node2 → node1 produced nothing
  over 10ms, and the reverse direction is clean too, so the paired
  node1→node2 / node2→node1 comparison above crosses a link known good in both
  directions. Best case matches on both nodes; node1's *variance* is what
  differs, which is why it surfaces at p95 and stays invisible in means.

  What is left unmeasured is the one path these tests skip: traffic entering
  node1 from another host through its NIC, ufw, conntrack, iptables DNAT and
  the Docker bridge. Loopback and container-IP requests bypass that chain
  entirely, which is exactly why node1 looks healthy when probed from itself.
  node2 has no equivalent — pasta forwards in userspace and firewalld never
  sees container traffic. Unproven, and the most likely candidate.

  Consequences for §2: **per-node p95 is not comparable between these nodes**,
  for reasons that predate any rollout. The zero-dropped-requests claim is
  unaffected — a slow response is still a successful one. Do not read a p95
  difference in the §2 evidence as something the rollout caused.

- **The workstation cannot be used as the load generator, despite reaching the
  lab LAN.** It was excluded originally on the belief that WSL2 was NAT'd off
  the LAN entirely; that was wrong — `ssh` and `curl` to the VIP both work. But
  measuring the path found episodic stalls specific to it: pinging node1 from
  the workstation produced `seq 21/22/23` at 214ms, 136ms and 60ms —
  consecutive and decaying, one ~400ms freeze with a queue draining behind it,
  not distributed jitter. The same workstation pings node2 cleanly, and node2
  pings node1 cleanly, so the fault is in the workstation↔node1 pair, where
  WSL2's NAT and the Windows host's neighbour table sit outside Linux's view.
  Notably the VIP (`.245`, node1's same NIC) stays clean, which fits: keepalived
  gratuitously ARPs it continuously, so that entry never goes stale.

  So k6 stays on node2 and the contamination caveat in §2 stands. Recorded
  because "the workstation is off the LAN" was load-bearing and false, and the
  replacement reason is different and needs to be the one on record.

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
