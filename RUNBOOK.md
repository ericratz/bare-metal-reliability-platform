# Runbook

Operational procedures for the two-node platform:

1. [First-time deployment](#1-first-time-deployment) — per-node, OS-specific
2. [Zero-downtime rolling update](#2-zero-downtime-rolling-update)
3. [VIP failover drills](#3-vip-failover-drills)

All client-facing traffic and all testing go at the **VIP `192.168.71.245`**,
never a node IP — hitting a node directly bypasses the thing under test.

Nodes: **node1** `192.168.71.251` (Ubuntu 26.04, Docker), **node2**
`192.168.71.252` (Rocky 10.2, Podman). keepalived priorities: node1 110, node2
100, so node1 holds the VIP when both are healthy.

---

## 1. First-time deployment

### 1·0. Get this repo onto both nodes

Everything below copies from `deploy/`, including the drop-in install in the
next section, so nothing in §1 works until each node has a clone.

```bash
git clone https://github.com/ericratz/bare-metal-reliability-platform.git
cd bare-metal-reliability-platform
cp .env.example .env          # set NODE_NAME — node1 or node2
```

> **Push from the workstation first.** A clone fetches what is on the remote,
> not what is in your working tree — deploying from a stale clone is the
> failure where every command succeeds and the config is silently a version
> behind. `deploy/systemd/` is a new directory, so `git commit -a` skips it:
> it stages tracked files only, and untracked ones need an explicit `git add`.
>
> Re-pull on both nodes after any later change to `deploy/`, and remember the
> lesson from the monitor: a pull updates files in the clone, it does not
> install them. Copying to `/etc` and reloading is always a separate step.

### 1·1. Registering components as you deploy them

Each step below ends by registering what it just deployed with the health
monitor. That is not bookkeeping. `HEALTH_SERVICES` and `HEALTH_APP_ENDPOINTS`
start trimmed to what actually exists on the host, so the monitor's exit code
means "this node is healthy as configured" from the first run — rather than
sitting at a standing WARNING for the weeks it takes to build the platform,
which is how a monitor teaches everyone to ignore it.

The cost of that choice is precisely this: **a component deployed but never
registered is a component nobody is watching, and nothing will tell you.**
There is no reconciliation step that catches it later. The registration line is
part of the deploy step, not a follow-up.

Registration is a **drop-in**, never an edit to `health-monitor.service` — that
unit ships from the `linux-health-monitor` repo and is replaced wholesale on
its next install, taking in-place edits with it. The drop-in is pre-written per
node; install it once here, at the start:

```bash
# node1:
sudo install -D -m 644 deploy/systemd/health-monitor-node1.conf \
  /etc/systemd/system/health-monitor.service.d/10-fleet.conf
# node2:
sudo install -D -m 644 deploy/systemd/health-monitor-node2.conf \
  /etc/systemd/system/health-monitor.service.d/10-fleet.conf
sudo systemctl daemon-reload
```

**Then confirm nothing else is setting the same variables.** The monitor's own
repo ships per-node drop-ins too, and systemd merges every `*.conf` in that
directory — last writer wins, in lexicographic order. Digits sort before
letters, so `10-fleet.conf` loses to anything named `nodeN.conf`, and the
override is completely silent: the file you edited is present, correct, and
ignored.

Do not reason about filenames. Check the merged result, which is ground truth:

```bash
ls /etc/systemd/system/health-monitor.service.d/
systemctl show health-monitor.service -p Environment --no-pager | tr ' ' '\n' | grep HEALTH_
```

Expect exactly one `.conf`, and `HEALTH_SERVICES` showing the trimmed baseline
— not the shipped end state. A name ending `.disabled` is inert; the drop-in
glob is `*.conf` and does not match it.

**If a second drop-in is present, read it before deleting it.** It may carry
settings this file does not, including non-`Environment=` directives — node2's
original `node2.conf` set `ProtectHome=no`, which `HEALTH_CONTAINER_USER`
requires to work at all. Absorb anything node-specific into this file, then
remove the other one. Deleting blind trades a loud collision for a silent gap.

Expect the override to be *partial*, which is the shape that fools you: on
node2 the second drop-in won `HEALTH_SERVICES` while `HEALTH_APP_ENDPOINTS`
and `HEALTH_VIP` still came from this file, so the file looked like it was
working. Read the merged values, never the file you last edited.

This file is authoritative for `HEALTH_SERVICES`, `HEALTH_APP_ENDPOINTS` and
`HEALTH_VIP` for as long as the platform is being deployed, because it is the
only one that knows which components exist yet.

It installs with only the baseline entries active. Every later entry is already
written and commented, tagged with the step that enables it — so each
**Register** step below is "move the active line down one," not "compose a
config from memory." Keep exactly one line active per variable.

After every registration:

```bash
sudo systemctl daemon-reload && sudo systemctl start health-monitor.service
systemctl is-failed health-monitor.service      # expect: inactive
```

If that does not come back clean, fix it before starting the next step. The
return on trimming the lists is a baseline you can trust, and it is only worth
anything if you check it at each stage rather than at the end.

### 1a. Firewall — do this first, or everything below silently fails

The two nodes ship different firewalls, and the failure mode is the same on
both: connections hang or refuse with no application-level error, so you debug
the app for an hour before checking the firewall. Open the ports first.

Traffic that must be allowed:

| Port / proto | From | For |
|---|---|---|
| 80/tcp | anywhere on LAN | nginx ingress (via VIP) |
| 8000/tcp | both nodes | app; nginx upstream + Prometheus scrape |
| 9090/tcp | both nodes | Prometheus; peer scrape |
| 9090/tcp | node1's container subnet | `/slo` → local Prometheus; **node1 only**, added at §1d |
| VRRP (proto 112) | the other node | keepalived adverts |

**node1 (Ubuntu, ufw):**

> ### Allow SSH first, or you will lock yourself out of a headless node
>
> ufw's default policy is **deny incoming**, and it ships with nothing
> allowed — so enabling it without an SSH rule closes port 22. `--force`
> exists specifically to suppress ufw's own *"Command may disrupt existing ssh
> connections. Proceed?"* prompt, which is the guard that would otherwise stop
> you here.
>
> The damage is delayed and therefore easy to miss: your **current** session
> survives on the ESTABLISHED rule, so nothing appears wrong. The lockout only
> shows up at the next login, on a minimized box with no desktop — physical
> access is the way back.
>
> node2 needed no equivalent because firewalld ships `ssh` in its default zone
> (visible as `services: cockpit dhcpv6-client ssh` in its `--list-all`). ufw
> ships nothing. Same intent, opposite defaults.

```bash
# ufw is not guaranteed present — this image is minimized, which is also why
# arping was missing earlier
command -v ufw || sudo apt-get install -y ufw

# SSH BEFORE ENABLE. Not optional, not reorderable.
sudo ufw allow 22/tcp

sudo ufw allow 80/tcp
sudo ufw allow from 192.168.68.0/22 to any port 8000 proto tcp
sudo ufw allow from 192.168.68.0/22 to any port 9090 proto tcp
sudo ufw --force enable && sudo ufw status verbose
```

Confirm `22/tcp ALLOW IN` appears in the `status verbose` output **before you
close the session** — while you still have a working connection to fix it
from. Once that is verified you can narrow it to
`sudo ufw allow from 192.168.68.0/22 to any port 22 proto tcp` and delete the
broad rule; do it in that order, never the reverse.

**VRRP cannot be expressed as a `ufw allow` rule at all.** ufw's `proto`
keyword accepts only `ah`, `esp`, `gre`, `igmp`, `ipv6`, `tcp` and `udp` —
there is no `vrrp` keyword and no numeric-protocol form, so
`ufw allow from <peer> proto vrrp` is rejected rather than silently ignored.
firewalld's `--add-protocol=vrrp` simply has no ufw counterpart, and this is
the one place the two nodes' firewalls are not merely spelled differently.

It has to go in the raw iptables rules ufw loads ahead of its own chains:

```bash
sudoedit /etc/ufw/before.rules
```

Add these lines inside the `*filter` section — anywhere after
`# End required lines` and before the closing `COMMIT`:

```
# VRRP (IP protocol 112) — keepalived adverts from the peer
-A ufw-before-input -p 112 -s 192.168.71.252 -j ACCEPT
```

> **node1 has no editor.** The minimized Ubuntu image ships no `vi`, `nano` or
> `ed`, so `sudoedit` fails with `failed to run editor /usr/bin/vi`. Either
> `sudo apt-get install -y nano` first, or apply it without one — this anchors
> on a marker present in every stock `before.rules`, and keeps a rollback:
>
> ```bash
> sudo cp /etc/ufw/before.rules /etc/ufw/before.rules.bak
> sudo awk '/^# End required lines/{print; print ""; print "# VRRP (IP protocol 112) — keepalived adverts from the peer"; print "-A ufw-before-input -p 112 -s 192.168.71.252 -j ACCEPT"; next} 1' /etc/ufw/before.rules.bak | sudo tee /etc/ufw/before.rules >/dev/null
> ```
>
> Third missing tool on this image after `arping` and `ufw` — assume nothing is
> installed here and check before depending on it.

```bash
sudo ufw reload
sudo iptables -S ufw-before-input | grep 112
```

The `grep` must return the rule. `ufw status` will **never** show it — that
only lists ufw's own rules, not `before.rules` — so this is the only
confirmation you get, and its absence is exactly the silent VRRP block that
produces split brain at §1e.

**node2 (Rocky, firewalld):**
```bash
sudo firewall-cmd --permanent --add-port=80/tcp
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --permanent --add-port=9090/tcp
# VRRP is IP protocol 112, not a port — --add-port cannot express it
sudo firewall-cmd --permanent --add-protocol=vrrp
sudo firewall-cmd --reload && sudo firewall-cmd --list-all
```

> If VRRP is blocked on either side, each node stops hearing the other's
> adverts, both conclude the peer is gone, and **both claim the VIP** — split
> brain. Traffic then goes to whichever node the switch's ARP cache learned
> last. This is the single most common keepalived bring-up failure.

### 1b. App container

**Set `APP_VERSION` in `.env` on both nodes**, not just `NODE_NAME`. It is what
`/health` reports, and it is the only evidence that a rolling update actually
landed. `.env.example` ships `dev`; the deployed release is `0.1.0`.

**node1 (Docker):**
```bash
cd ~/bare-metal-reliability-platform
cp .env.example .env          # set NODE_NAME=node1 and APP_VERSION=0.1.0
docker compose up -d --build
curl -s http://127.0.0.1:8000/health | jq '{node, version, status}'
docker compose ps             # STATUS must reach (healthy), not just Up
```

**node2 (Podman + Quadlet).** Podman is daemonless, so a plain
`podman run --restart` does not survive reboot — nothing is watching to restart
it. The systemd Quadlet unit is what makes the container a managed service.
Full steps (build, linger, install) are in `deploy/podman/README.md`. In brief:
```bash
cd ~/bare-metal-reliability-platform
cp .env.example .env          # set NODE_NAME=node2 and APP_VERSION=0.1.0
podman build --format docker --build-arg APP_VERSION=0.1.0 -t brp-api:latest .
mkdir -p ~/.config/containers/systemd
cp deploy/podman/brp-api.container ~/.config/containers/systemd/
sudo loginctl enable-linger "$USER"   # or the unit dies on logout
systemctl --user daemon-reload && systemctl --user start brp-api
sleep 15
curl -s http://127.0.0.1:8000/health | jq '{node, version, status}'
podman ps --format '{{.Names}}\t{{.Status}}'   # Status must show (healthy)
```

> **Two node2-only traps, both cases of Docker hiding what Podman surfaces.**
> Neither announces itself as an error.
>
> `--format docker` is **required**. The OCI image format has no healthcheck
> field, so a default `podman build` discards the Dockerfile's `HEALTHCHECK`
> and says so only in a build warning you will scroll past. node1's Docker
> build keeps it. Without the flag the two nodes run different artifacts from
> one Dockerfile, and node2's container has no health gating at all.
>
> Verify with `http://127.0.0.1:8000/health`, **never `localhost`**. Rootless
> Podman forwards with pasta, which accepts on `::1` and has nothing behind it,
> so a v6 connection completes its handshake and is then reset. `localhost`
> resolves `::1` first on Rocky, and because the connect succeeded the client
> does not fall back to v4 — `curl -s` prints an empty body and exits, which
> reads as "the app is down" when it is serving fine.
>
> The Quadlet passes `--ipv4-only` to pasta so `::1` refuses cleanly and clients
> fall back, but address the literal anyway rather than depending on that.
> Setting a bind address on `PublishPort` does **not** work — podman drops it
> when building pasta's forwarding spec. Confirm the flag on the process, since
> neither the unit file nor `podman ps` will tell you:
>
> ```bash
> pgrep -a pasta | grep -- --ipv4-only && ss -ltn | grep 8000
> ```
>
> `ss` must show `0.0.0.0:8000`. A `*:8000` there means the wildcard bind is
> back and the reset with it.

> **SELinux (node2).** If the container cannot bind its port or is denied at
> startup, check `sudo ausearch -m avc -ts recent`. Do not blanket-disable
> SELinux — that throws away the exact portability signal this project exists to
> surface. Add the specific policy the denial names, and record it as a
> node2-only deployment step.

Verify each node answers on its own IP before wiring anything together:
```bash
curl -s http://192.168.71.251:8000/health | jq .node   # -> "node1"
curl -s http://192.168.71.252:8000/health | jq .node   # -> "node2"
```

**Register.** Uncomment the `#§1b` line under *app endpoints* in the drop-in:
this node's own `http://127.0.0.1:8000/health`, plus the peer's. The peer's is
reported but deliberately **not** scored, so a peer outage does not mark both
units failed and destroy the one signal that says which node to look at.

`HEALTH_SERVICES` needs no change at this step, and on node2 that is a decision
rather than an oversight:

- **node1:** `docker` is already active in the baseline — the daemon predates
  any of this work. Compose containers are not units, so the daemon is the
  closest thing systemd can watch.
- **node2:** nothing is added for the container, here or ever. `brp-api.service`
  is a rootless *user* unit generated by Quadlet, and `systemctl --failed` at
  the system level cannot see it, so registering it would report a unit that is
  permanently not-installed — a standing WARNING that never clears. Podman is
  daemonless, so there is no daemon unit to watch either. The endpoint above
  covers this container, and covers it better: it tests that the app answers,
  not that a process exists.

### 1c. nginx (both nodes)

**Validate the config before installing it.** §1c is gated on this because
`brp.conf` changes between deploys and a bad one takes ingress down on the node
you are standing on. The workstation has no docker daemon — only the Docker
Desktop client shim, no `dockerd` and no socket — so run it on node1:

```bash
cd ~/bare-metal-reliability-platform
docker run --rm -v "$PWD/deploy/nginx:/etc/nginx/conf.d:ro" nginx:alpine nginx -t
```

Mount the whole directory, not the two files — it replaces the image's own
`default.conf`, which otherwise collides with `listen 80 default_server` and
makes the test fail for a reason that does not exist on the nodes.

**Install nginx.** Not present on either node; node1's image is minimized, so
assume nothing:

```bash
command -v nginx || sudo apt-get install -y nginx    # node1
command -v nginx || sudo dnf install -y nginx        # node2
```

**Remove the distro's default server, or `nginx -t` fails on both nodes** —
`duplicate default server for 0.0.0.0:80`. Both packages ship a server block
listening on `:80 default_server`, and they ship it in different places, so the
fix is not portable:

```bash
# node1 (Ubuntu) — a separate file, and nginx.conf includes sites-enabled/*
sudo rm -f /etc/nginx/sites-enabled/default

# node2 (Rocky) — the block lives INSIDE /etc/nginx/nginx.conf, no file to unlink
sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak
sudo sed -i '/^    server {/,/^    }/d' /etc/nginx/nginx.conf
```

Check node2's result before trusting it — that `sed` deletes the first
indented `server` block, and a package update could change the indentation
under it:

```bash
grep -n 'server {\|listen' /etc/nginx/nginx.conf   # expect no server block left
```

`nginx.conf.bak` is safe to leave in place: nginx includes `conf.d/*.conf`, not
`/etc/nginx/*`, so it is inert. Restore from it if the grep looks wrong.

**Install the configs and the report directory:**

```bash
# Debian: /etc/nginx/conf.d/ ; RHEL: same path, both use conf.d
sudo cp deploy/nginx/brp-upstream.conf deploy/nginx/brp.conf /etc/nginx/conf.d/
# target of location = /report — setgid is required, see below the block
sudo install -d -o root -g www-data -m 2750 /var/www/health   # node1 (Ubuntu)
sudo install -d -o root -g nginx    -m 2750 /var/www/health   # node2 (Rocky)
```

**SELinux — node2 only, and this is the step that decides whether the proxy
works at all.** Two separate denials, neither of which produces a useful error:

```bash
# 1. nginx may not make outbound connections by default under SELinux, so
#    every proxy_pass to :8000 fails and the node serves 502s while
#    /nginx-health still answers 200 — i.e. keepalived sees a healthy LB
#    in front of a proxy that cannot reach anything.
sudo setsebool -P httpd_can_network_connect 1

# 2. /var/www is not RHEL's web root (/usr/share/nginx/html is), so files
#    created there do not carry httpd_sys_content_t and nginx is denied
#    reading the report — a 403, indistinguishable at a glance from the
#    wrong-group 403 described below.
command -v semanage || sudo dnf install -y policycoreutils-python-utils
sudo semanage fcontext -a -t httpd_sys_content_t "/var/www/health(/.*)?"
sudo restorecon -Rv /var/www/health
```

node1 needs neither: AppArmor's nginx profile permits both, so this is another
place where the same config is correct on one node and silently non-functional
on the other. If something is denied anyway, read the AVC rather than disabling
SELinux — `sudo ausearch -m avc -ts recent`.

**Enable and verify.** The explicit `restart` is not redundant — see below:

```bash
sudo nginx -t && sudo systemctl enable --now nginx && sudo systemctl restart nginx
curl -s http://127.0.0.1/nginx-health    # -> "nginx ok"  (nginx itself, never proxied)
curl -s http://127.0.0.1/health | jq '{node, status}'   # proxied to a backend
```

> **`enable --now` is not enough on node1, and neither is `reload`.** Debian
> packages start a service on install, so nginx is already running before the
> config is in place, and `--now` does nothing to an active unit. What you get
> is `is-active: active`, `nginx -t` succeeding, `nginx -T` printing the new
> config, and the stock default site still being served: 404 on
> `/nginx-health`, HTML instead of JSON on `/health`. Every indicator reads
> healthy while the config you installed is not the one answering.
>
> `systemctl reload nginx` did **not** fix it — observed, not explained; the
> package's install-time `Upgrading binary nginx` step is the likely reason,
> since a binary upgrade leaves workers parented differently than a plain
> start. A full `restart` did. Note `nginx -T` cannot detect this at all: it
> reads config from disk rather than from the running master, so it will
> happily confirm a config that is not loaded.
>
> RHEL packages do not auto-start, so on node2 `enable --now` genuinely starts
> nginx against the right config. The `restart` makes one sequence correct on
> both without branching. This is also why the `/nginx-health` check below is a
> real verification and not a formality — it is the only step in §1c that
> distinguishes "nginx is running" from "nginx is running *your* config."

Run the proxied `curl` a few times on each node — `node` should vary between
node1 and node2, confirming the upstream reaches both.

If it never varies, the upstream is only reaching one node: check
`brp-upstream.conf` and §1a. Do not "fix" it by pinning `/health` to the local
app — every evidence tool in this repo attributes requests by reading the
`node` field out of a `/health` response, and pinning it silently reduces all
of them to a single node. The reasoning is written out at the top of
`deploy/nginx/brp.conf`.

**Register.** Add `nginx.service` to `HEALTH_SERVICES` on both nodes.

> `location = /report` serves 404 until the monitor writes there. `--html` now
> accepts an absolute path and creates the parent tree itself, but the unit
> still ships the default (`/opt/linux-health-monitor/reports/report.html`),
> deliberately — it is what the existing `grep`-based checks target. **This is
> the step that flips it**, in the same drop-in as the registration above:
>
> ```
> Environment=... --html /var/www/health/report.html
> ```
>
> **The `install -d` above is mandatory, and the `2750` is the whole trick.**
> The monitor writes the report `0640` root-owned — deliberately, so a local
> account cannot read a document that enumerates listening ports, process
> names, containers, addressing and MACs. nginx therefore cannot read it unless
> it is in the owning group, and because the file is rewritten every cycle a
> one-off `chgrp` is erased on the next run. The setgid bit makes each new
> report inherit the directory's group instead.
>
> The group differs by node: `www-data` on Ubuntu, `nginx` on Rocky. Getting it
> wrong gives a 403 on `/report`, not a 404 — that distinction is the fastest
> way to tell "wrong group" from "monitor has not written yet."
>
> Publishing the report is meant to be a deliberate act here, not something a
> default umask decides.

### 1d. Prometheus (both nodes)

**Installed from the upstream tarball on both nodes, not from a package.**
Ubuntu has `prometheus` in universe (2.53.x); Rocky 10 has it in no enabled
repo, and EPEL dropped it after EL8. Package-where-available would put a 2.x
collector on node1 and a 3.x on node2 — and since the hardware is controlled
for precisely so that per-node metric differences are attributable to the OS,
two different collectors would give every such difference a second candidate
explanation. Same version on both is the point. The reasoning is repeated in
`deploy/prometheus/prometheus.service`, which is where someone will actually
find it.

**The trade is stated rather than hidden: this binary gets no distro security
updates.** `unattended-upgrades` and `dnf update` will never touch it. Upgrading
is re-running the download-and-install step below with a new version, on both
nodes, and it is on you to notice a CVE. That is the cost of fleet consistency
here; a single-OS fleet would not have to pay it.

**Pick one version and use it on both nodes:**

```bash
curl -s https://api.github.com/repos/prometheus/prometheus/releases/latest | jq -r .tag_name
```

Prefer an LTS line if one is current — upgrades are manual, so a longer support
window is worth more here than the newest features.

**Install the binaries — run on BOTH nodes with the same `PROM_VER`:**

```bash
PROM_VER=<the version above, without the leading v>
cd /tmp
curl -sLO https://github.com/prometheus/prometheus/releases/download/v${PROM_VER}/prometheus-${PROM_VER}.linux-amd64.tar.gz
curl -sLO https://github.com/prometheus/prometheus/releases/download/v${PROM_VER}/sha256sums.txt
sha256sum -c --ignore-missing sha256sums.txt
tar xzf prometheus-${PROM_VER}.linux-amd64.tar.gz
sudo install -m 755 prometheus-${PROM_VER}.linux-amd64/prometheus prometheus-${PROM_VER}.linux-amd64/promtool /usr/local/bin/
prometheus --version
```

The checksum step is not decoration — this is the one component in the platform
installed by downloading a binary over the network instead of from a signed
repository, so it is the one place where verifying what you got is on you.
`--ignore-missing` is required because `sha256sums.txt` covers every platform.

**User, directories and config — both nodes:**

```bash
id prometheus >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin prometheus
sudo install -d -o root -g root -m 755 /etc/prometheus/rules
sudo install -d -o prometheus -g prometheus -m 750 /var/lib/prometheus
cd ~/bare-metal-reliability-platform
sudo cp deploy/prometheus/prometheus.yml /etc/prometheus/
sudo cp deploy/prometheus/rules/*.yml /etc/prometheus/rules/
sudo install -m 644 deploy/prometheus/prometheus.service /etc/systemd/system/
```

**node2 only — the one line that differs, and one SELinux relabel:**

```bash
sudo sed -i 's|^    replica: node1$|    replica: node2|' /etc/prometheus/prometheus.yml
grep -n 'replica:' /etc/prometheus/prometheus.yml      # must read node2
sudo restorecon -v /usr/local/bin/prometheus /usr/local/bin/promtool
```

Do not skip the `grep`. Both instances silently claiming `replica: node1` is
not an error anywhere — it produces two datasets that are indistinguishable the
moment you compare them, which is the only reason the label exists.

**Validate, then start — both nodes:**

```bash
/usr/local/bin/promtool check config /etc/prometheus/prometheus.yml
sudo systemctl daemon-reload && sudo systemctl enable --now prometheus && sudo systemctl restart prometheus
systemctl is-active prometheus
```

`promtool check config` follows `rule_files` and validates the rules too, so it
covers `/etc/prometheus/rules/*.yml` without a separate `check rules` run. The
explicit `restart` is for the same reason as §1c — `--now` is a no-op if the
unit is somehow already active, and here it costs nothing.

> **No `sudo`, and an absolute path — both deliberate.** The config is
> world-readable, so root is not needed. More importantly `sudo promtool` works
> on node1 and fails with `command not found` on node2: RHEL's sudoers
> `secure_path` is `/sbin:/bin:/usr/sbin:/usr/bin`, which excludes
> `/usr/local/bin`, while Debian's includes it. The binary is installed
> identically on both nodes and is on the interactive `PATH` on both — it is
> only invisible *through sudo* on node2.
>
> `prometheus.service` is unaffected because `ExecStart` is already absolute.
> Worth knowing beyond this step: any locally-installed tool invoked with
> `sudo` on node2 needs its full path, and the error names the command rather
> than the cause, so it reads like a failed install.

If node2 fails to start where node1 succeeded, read the denial before assuming
the unit is wrong: `sudo journalctl -u prometheus -n 30 --no-pager` and
`sudo ausearch -m avc -ts recent`. A binary under `/usr/local/bin` is the kind
of path SELinux has opinions about.

Confirm at `http://<node>:9090/targets` — both nodes should show 2 UP `brp-api`
targets and 2 UP `prometheus` targets, from each node's point of view. Or from
a shell on either node:

```bash
curl -s 'http://127.0.0.1:9090/api/v1/targets?state=active' | jq -r '.data.activeTargets[] | "\(.labels.job) \(.labels.node) \(.health)"'
```

Four lines, all `up`. Then confirm the two instances disagree about who they
are, which is what proves the `replica` edit landed on the running config
rather than only on disk:

```bash
curl -s http://127.0.0.1:9090/api/v1/status/config | jq -r .data.yaml | grep -A3 external_labels
```

`node1` on node1, `node2` on node2. Note this is the *only* way to see it from
Prometheus: `external_labels` are attached to federation, remote-write and
alerts — never to local query results — so `query=up` will not show them and is
not a check.

**Point the app at Prometheus — it is not reachable from the container yet.**
`/slo` will report `status: unavailable` until this is done, and that report is
accurate: the app runs in a container, Prometheus runs on the host, and the
original `PROM_URL=http://127.0.0.1:9090` names the container's own loopback
where nothing listens. This was wrong from §1b onward and only became visible
here, because before §1d there was no Prometheus to fail to reach.

`host.docker.internal` resolves to the host under both runtimes — Podman
aliases it natively, and `docker-compose.yml` maps it with
`extra_hosts: host-gateway` — so `PROM_URL` is one identical value on both
nodes.

**node1 (Docker + ufw).** Needs a firewall rule as well, and the container must
be recreated because the Compose network's subnet is now pinned:

```bash
# §1a's 9090 rule allows 192.168.68.0/22 only; the container's source address
# is on the Docker bridge, so container-to-host :9090 is DROPPED — a timeout,
# not a refusal, which is what makes it read as a hung Prometheus.
sudo ufw allow from 172.28.0.0/24 to any port 9090 proto tcp
sudo ufw status | grep 9090

cd ~/bare-metal-reliability-platform
sed -i 's|^PROM_URL=.*|PROM_URL=http://host.docker.internal:9090|' .env
# down, not restart: the pinned `networks:` subnet only applies on recreate
docker compose down && docker compose up -d
```

**node2 (Podman).** Config only — pasta routes the container to the host
directly and firewalld never sees the traffic:

```bash
cd ~/bare-metal-reliability-platform
sed -i 's|^PROM_URL=.*|PROM_URL=http://host.docker.internal:9090|' .env
systemctl --user restart brp-api
```

**Verify on both before registering:**

```bash
curl -s http://127.0.0.1:8000/slo | jq
```

`status` must not be `unavailable`. That is the whole gate — the numbers are
not part of it.

Note what a correct-but-idle answer looks like under 0.2.0, so it is not
mistaken for a fault. With no traffic, every metric is `null` and named in
`no_data`, and `status` is still `ok`:

```json
{ "status": "ok", "node": "node1", "window": "2m",
  "availability_percent": null, "p95_latency_ms": null,
  "no_data": ["availability_percent", "p95_latency_ms"] }
```

That is the endpoint working. `null` here means "nothing has been served in the
last two minutes", which at this point in the deploy is true and expected. An
unreachable Prometheus looks entirely different — `status: unavailable`, and
the metric keys are absent rather than null.

> If you are following this runbook on a node still running `0.1.0`, the idle
> answer is `availability_percent: 100.0` instead — a fabricated number this
> check used to instruct you to accept. That is the defect `0.2.0` fixes; see
> `CHANGELOG.md`.

**Register.** Add `prometheus.service` to `HEALTH_SERVICES`, and this node's
`http://127.0.0.1:8000/slo` (the `fleet-slo` entry) to `HEALTH_APP_ENDPOINTS`
— not before now, and not before the check above passes. `/slo` reads from
local Prometheus and degrades to `status: unavailable` without it, so
registering it any earlier books a guaranteed failure against the baseline you
are trying to keep quiet.

### 1e. keepalived (both nodes)

> **On the DHCP pool.** The router's admin UI is not available, so `.245`
> cannot be proven outside the DHCP pool. It was chosen on the evidence that
> is available, and the residual risk is handled by detection rather than
> prevention. Both are written down here because the failure, if it comes,
> arrives months from now with nothing recent to blame.
>
> **Why `.245` is a sound choice anyway.** Three addresses already sit at the
> top of this range — the appliance on `.250`, node1 `.251`, node2 `.252` —
> and have coexisted with DHCP without incident. That is empirical evidence
> that the pool does not reach the top of the `/22`, which is worth more than
> a config screenshot. `.245` sits in the same band, five below the lowest of
> them, and answered nothing from either node.
>
> **Why a live VIP largely defends itself.** RFC 2131 §4.3.1 says a DHCP
> server SHOULD probe an address before offering it, and skip it if something
> answers. keepalived answers ARP for `.245` continuously, so a conforming
> router will not lease it out while the platform is up. The exposure window
> is narrow and specific: **both nodes down at once** — a power cut, or
> maintenance on both — long enough for the router to probe, find silence, and
> lease it. Then the nodes return and conflict.
>
> **How you find out, if it ever happens.** The health monitor reports
> `answered_by` for the VIP on every cycle. On the backup node that MAC should
> be the holder's; anything else means another device has claimed `.245`, and
> it surfaces within one interval instead of months of intermittent symptoms.
> That detection is only usable if you can recognise the MACs, so record them
> now — this is the step that makes the whole mitigation real:

```bash
# run on both nodes; keep the output with the runbook
echo "$(hostname) $(cat /sys/class/net/enp1s0/address)"
```

Recorded 2026-07-30:

| Host | Interface | MAC |
|---|---|---|
| node1 | `enp1s0` | `24:1c:04:14:42:ce` |
| node2 | `enp1s0` | `24:1c:04:14:44:7c` |
| appliance squatting `.250` | — | `d8:44:89:a0:66:60` |

Both nodes share the `24:1c:04` OUI — identical hardware, so this is expected
and it makes the check easy to apply from memory: **a VIP `answered_by` that
does not start `24:1c:04` is not one of ours.** `d8:44:89:…` is the appliance,
correct on `.250` and never legitimate on `.245`.

> If `.245` ever does collide, pick another address in the same top band,
> re-run the `ip neigh` check from both nodes, and grep — the VIP appears in
> seven files, so do not edit from memory.

**Pre-flight — all three checks, on both nodes, before installing anything.**
This is the only step in §1 that can take a working platform down rather than
merely fail to come up, and each of these failures is silent:

```bash
# 1. The interface name is hardcoded in both keepalived configs, in the MAC
#    table above, and in every verification below. Confirm it is enp1s0.
ip -br link | grep -v LOOPBACK

# 2. VRRP must be permitted or BOTH nodes claim the VIP. node1's rule lives in
#    before.rules and `ufw status` will NEVER show it — this grep is the only
#    confirmation that exists.
sudo iptables -S ufw-before-input | grep 112          # node1
sudo firewall-cmd --list-protocols                    # node2 — expect vrrp

# 3. Re-check that .245 is still unanswered. The earlier check was days ago and
#    a DHCP lease could have landed on it since. Run from BOTH nodes.
#    node1's image ships no ping — fifth missing tool there after arping, ufw,
#    an editor and jq. Install it (the §3 drills want it) or trigger the ARP
#    resolution with curl instead; what matters is the neighbour state, not
#    which tool provoked it.
command -v ping || sudo apt-get install -y iputils-ping    # node1
ping -c2 -W1 192.168.71.245; ip neigh | grep 192.168.71.245
```

Check 3 must find nothing — no reply, and a neighbour entry that is
`INCOMPLETE` or `FAILED` rather than one naming a MAC. If something answers, **stop** — assigning the VIP
on top of a live host produces an intermittent conflict that is far harder to
diagnose than this check is to run. Pick another address in the same top band
and grep: the VIP appears in seven files, so do not edit from memory.

**Install keepalived.** Not present on either node:

```bash
command -v keepalived || sudo apt-get install -y keepalived    # node1
command -v keepalived || sudo dnf install -y keepalived        # node2
```

**Install the config and scripts:**

```bash
cd ~/bare-metal-reliability-platform
# node1:
sudo install -m 644 deploy/keepalived/keepalived-node1.conf /etc/keepalived/keepalived.conf
# node2:
sudo install -m 644 deploy/keepalived/keepalived-node2.conf /etc/keepalived/keepalived.conf
# both — root-owned and 755, or enable_script_security refuses to run them:
sudo install -m 755 -o root -g root deploy/keepalived/check_nginx.sh deploy/keepalived/notify.sh /etc/keepalived/
ls -l /etc/keepalived/
```

**Bring node1 up FIRST, alone, and confirm it takes the VIP.** Order matters
here and the reason is `nopreempt`: whichever node reaches MASTER first keeps
the VIP, because a nopreempt BACKUP will not take over from a healthy peer even
at higher priority. Start node2 first and you get a working platform with the
roles inverted from every diagram and drill in this repo.

```bash
# node1 only:
sudo systemctl enable --now keepalived && sudo systemctl restart keepalived
sleep 5
ip addr show enp1s0 | grep 192.168.71.245        # MUST show the VIP
journalctl -t keepalived-notify -n5 --no-pager   # expect state=MASTER
```

The explicit `restart` is for the same reason as §1c and §1d — Debian starts a
service on install, so keepalived may already be running against a config that
did not exist yet, and `--now` is a no-op on an active unit.

**Then node2, and confirm it does NOT take the VIP:**

```bash
# node2 only:
sudo systemctl enable --now keepalived && sudo systemctl restart keepalived
sleep 5
ip addr show enp1s0 | grep 192.168.71.245        # MUST print NOTHING
journalctl -t keepalived-notify -n5 --no-pager   # expect state=BACKUP
```

**If the VIP appears on both nodes, that is split brain** — VRRP adverts are
not getting through, and pre-flight check 2 is where to look. Traffic then goes
to whichever node the switch's ARP cache learned last, so it will *appear* to
work intermittently. Stop keepalived on node2 and fix the firewall before
continuing.

**Verify the VIP actually serves.** This one does work from the workstation —
it routes to the lab LAN and reaches `.245` in ~15ms, so a failure here is a
real failure, not an artifact of where you ran it. What still must run on a
node is anything at layer 2 (`arping`, `ip neigh`, duplicate-address checks),
which does not survive the routed hop:

```bash
curl -s http://192.168.71.245/health | jq '{node, status}'
curl -s http://192.168.71.245/nginx-health
```

`node` may be either node1 or node2 — the VIP lands on node1's nginx, which
balances across both backends. That is correct, not a fault.

> **node2 and SELinux — check this even though the platform looks fine.**
> keepalived runs confined in `keepalived_t` on RHEL and node1 has no
> equivalent confinement, so every script keepalived runs is subject to denials
> that simply do not exist on node1:
>
> ```bash
> sudo journalctl -u keepalived -n 30 --no-pager | grep -i 'script\|fault\|fail'
> sudo ausearch -m avc -ts recent
> ```
>
> Two denials are expected here, and they are not equally serious:
>
> - **`getattr` on `/usr/bin/hostname` (`hostname_exec_t`)** — `keepalived_t`
>   may not even stat that binary, so `$(hostname)` inside a notify script
>   returns empty. This is why `notify.sh` uses the `$HOSTNAME` bash builtin
>   instead. Fixed at the source; if you see this AVC, the deployed script is
>   stale.
> - **`setattr` on `check_nginx.sh` / `notify.sh` (`etc_t`)** — keepalived tries
>   to set attributes on its scripts and is refused, because installing into
>   `/etc/keepalived/` labels them `etc_t`. **Benign:** the log will show
>   `VRRP_Script(check_nginx) succeeded` alongside it, and the notify script
>   demonstrably runs. Audit noise, not a fault. Left unfixed deliberately —
>   adding a local policy module to silence a denial that breaks nothing trades
>   a harmless log line for custom policy nobody will remember writing.
>
> What this step is really checking for is a `check_nginx.sh` that *fails* on
> node2, and that failure does not look like an error: node2 is the backup,
> holding nothing is its correct state, and a permanently-failing check just
> means it can never take over. The platform looks healthy right up until the
> failover you needed does not happen. `VRRP_Script(check_nginx) succeeded` in
> the journal is the only thing that rules it out.

**Register — last step, and the one that completes the baseline.** On both
nodes add `keepalived.service` to `HEALTH_SERVICES`, set `HEALTH_VIP` to the
VIP, and add the `vip=` entry to `HEALTH_APP_ENDPOINTS`.

The `vip=` endpoint is scored on **both** nodes, unlike a peer's endpoint: a
VIP that does not answer is a fleet-level fault, and every node should say so
regardless of which one is currently holding it. `HEALTH_VIP` is what makes
that classification work, so setting it is not optional here.

Holding nothing is the correct state for the backup node and never degrades its
health — expect `held: false` on node2 with `answered_by` naming node1's MAC.
An `answered_by` that matches neither node means something else on the LAN has
claimed the address.

With this step done, both nodes should run the monitor and exit 0. If they do
not, the platform is not finished — which is now a signal you can trust,
because it is the first time the baseline has been quiet.

---

## 2. Zero-downtime rolling update

Updating both app instances without dropping a request. ~15 minutes.

### What makes this work

`proxy_next_upstream` in `brp.conf`. When the node being updated refuses a
connection, nginx retries that request on the surviving node and the client
sees one clean 200. That retry is the zero-downtime mechanism — draining the
node first just keeps it from being needed at scale. Nginx OSS has **no active
health checking**; it does not poll `/health`, it only reacts to real requests
failing (`max_fails`/`fail_timeout`). So pool membership is managed explicitly
with `pool.sh` rather than trusted to be noticed.

### Preconditions

- [ ] Both nodes healthy: `curl -s http://192.168.71.25{1,2}:8000/health | jq .status`
- [ ] Both in the pool: `sudo ./scripts/pool.sh status` → both `[IN ]`
- [ ] New image built on both nodes (or built once and `podman/docker save | ssh … load`)
- [ ] Nothing else deploying
- [ ] **Both checkouts are at the commit you intend to deploy**, and the same one:

      ```bash
      for n in 251 252; do echo -n "node .$n: "; ssh ericratz@192.168.71.$n 'git -C ~/bare-metal-reliability-platform pull --quiet && git -C ~/bare-metal-reliability-platform rev-parse --short HEAD'; done
      ```

      Both must print the same hash. This is a gate, not housekeeping: `APP_VERSION`
      comes from `.env` and is set independently of what is in the checkout, so
      building a stale tree produces an image **tagged and reporting `0.2.0` while
      containing the old code**. Step 5 below gates on the version string and would
      pass it. Nothing downstream catches it either — the version is right, the
      container restarted, `uptime_seconds` is small. You would have a green
      rollout of nothing, and then debug the missing fix on the node that
      "deployed" it.

      A pull is not an install. Neither node's running container changes until it
      is rebuilt, which is step 4 and step 7.
- [ ] k6 installed on node2 — see below, it is not in Rocky's base repos
- [ ] You know what the new version *is*. 0.2.0 is the `/slo` no-data fix; see
      `CHANGELOG.md`. Step 5 gates on `version` changing, so a rebuild with no
      version bump passes the health check while proving nothing.

#### Installing k6 on node2

Not packaged for Rocky 10. Static binary from the upstream release, same
pattern as Prometheus in §1d — and with the same caveat: **no distro security
updates, upgrades are manual.** Acceptable here because k6 is an operator tool
run by hand, not a service listening on anything.

```bash
ssh ericratz@192.168.71.252
K6_VERSION=$(curl -fsSL --max-time 30 https://api.github.com/repos/grafana/k6/releases/latest | jq -r .tag_name)
echo "$K6_VERSION"     # sanity-check it looks like v1.2.3 before continuing
# --progress-bar, not -s. The tarball is tens of MB and node2's link is slow —
# this took ~3 minutes in practice. Silenced, a slow download is indistinguishable
# from a hung one, and the natural response is to Ctrl-C a working transfer.
curl -fL --progress-bar --connect-timeout 10 --max-time 600 \
  "https://github.com/grafana/k6/releases/download/${K6_VERSION}/k6-${K6_VERSION}-linux-amd64.tar.gz" \
  -o /tmp/k6.tar.gz
tar -xzf /tmp/k6.tar.gz -C /tmp
sudo install -m 0755 "/tmp/k6-${K6_VERSION}-linux-amd64/k6" /usr/local/bin/k6
sudo restorecon -v /usr/local/bin/k6
k6 version
```

If it appears to stall, check whether it is actually moving before killing it —
`ls -l /tmp/k6.tar.gz` twice, a few seconds apart. Note the download redirects
to `objects.githubusercontent.com`, so it costs a second DNS lookup on a
different name than the API call above.

If the download 404s, check the asset name on the releases page — the archive
layout is upstream's to change, and this is the one step here that depends on it.

### Step 0 — start evidence collection (before touching anything)

**Both of these run on node2, not on your workstation.** Not because the
workstation cannot reach the VIP — it can — but because it is a poor load
generator: the workstation↔node1 path has episodic ~400ms stalls (see
`CHANGELOG.md`), and a load generator whose own path stalls cannot distinguish
its jitter from the fleet's. Two SSH sessions to node2:

```bash
ssh ericratz@192.168.71.252
cd ~/bare-metal-reliability-platform
mkdir -p evidence      # k6 writes its summary here at exit and will not create it

# terminal A
./scripts/watch-uptime.sh http://192.168.71.245/health
# terminal B
k6 run -e BASE_URL=http://192.168.71.245 -e MAX_VUS=50 -e DURATION=15m k6/rolling-update.js
```

> **The load generator is also one of the backends under test.** There is no
> third machine on the lab LAN, so this is a constraint, not a choice. node2 is
> the less bad of the two: node1 holds the VIP *and* is the nginx every request
> passes through, so generating load there would put the client, the balancer,
> the VRRP master and a backend on one 2-core box.
>
> What it costs: node2's share of `p95_latency_ms` includes whatever k6 itself
> is consuming, so **per-node latency from this run is not a clean number** and
> should not be quoted as one. What it does *not* cost: the zero-dropped-requests
> claim, which is a count of failed responses and is indifferent to which host
> asked. `MAX_VUS=50` caps the ceiling so a latency blip cannot escalate into a
> self-inflicted load spike — see the comment in `k6/rolling-update.js`.

Both must still be running at the end. If either started late or died, the run
proves nothing — restart from here.

Leave these two sessions alone for the rest of the procedure. Every `ssh
ericratz@192.168.71.252` below is a *third* session — do not reuse terminal A
or B, and note that step 4 restarts the app container on the very node these
are running from. That is fine: they are pointed at the VIP on node1, which
serves them from node1's backend while node2's is down.

### Step 1 — record starting versions

```bash
curl -s http://192.168.71.251:8000/health | jq '{node, version, status}'
curl -s http://192.168.71.252:8000/health | jq '{node, version, status}'
```

Exactly one `version` should change per half of this procedure.

### Step 2 — drain node2

```bash
sudo ./scripts/pool.sh down node2
```

Comments node2 out of the upstream on **both** nodes' nginx — run it on each, or
keep the upstream file in sync. Syntax-checks and reloads gracefully.

### Step 3 — confirm node2 is receiving nothing

```bash
# on node1: send known-fresh traffic, then check only what it produced
for i in $(seq 20); do curl -s -o /dev/null http://192.168.71.245/health; done
sudo tail -20 /var/log/nginx/brp_access.log | grep -c 'upstream=192.168.71.252'   # -> 0
```

**Do not continue until this is 0.** Restarting a node still taking traffic is
the mistake this whole procedure exists to prevent.

> Two things here are deliberate and were both wrong in an earlier version.
>
> **`grep` must match `upstream=`, not the bare address.** The log line is
> `$remote_addr [...] upstream=$upstream_addr [...]`, so `192.168.71.252`
> appears both when node2 *served* a request and when node2 *sent* one — and
> node2 is where k6 and the runbook's own `curl` commands run. A bare grep
> counts your own client traffic as proof the drain failed.
>
> **Generate the traffic first.** `tail -20` is a line count, not a time
> window: on a quiet fleet those lines can all predate the drain, so a `0`
> would mean "nothing has happened recently", not "node2 is receiving
> nothing". Absence of evidence is not the check you want before restarting a
> service. Twenty requests you just sent make the window known-fresh, and turn
> this into a positive proof that live traffic is landing only on node1.

### Step 4 — update node2

```bash
ssh ericratz@192.168.71.252
cd ~/bare-metal-reliability-platform
git pull                       # the image is built from this tree; see preconditions
# .env FIRST: the Quadlet's EnvironmentFile injects APP_VERSION at runtime and
# that shadows what the build-arg baked in, so a rebuild alone leaves /health
# reporting the old version and step 5 below gates on a value nothing changed.
sed -i 's/^APP_VERSION=.*/APP_VERSION=0.2.0/' .env
# --format docker or the healthcheck is silently dropped — see §1b
podman build --format docker --build-arg APP_VERSION=0.2.0 -t brp-api:latest . && systemctl --user restart brp-api
```

### Step 5 — health-check node2 directly, before it takes traffic

```bash
curl -s http://192.168.71.252:8000/health | jq '{node, version, status, uptime_seconds}'
```

All four right: `status` healthy, `version` new, `node` node2, `uptime_seconds`
small (proves the container actually restarted, not the old one lingering).

### Step 6 — return node2 to the pool

```bash
sudo ./scripts/pool.sh up node2
# on node1, same shape as step 3 — fresh traffic, and match the upstream field
for i in $(seq 20); do curl -s -o /dev/null http://192.168.71.245/health; done
sudo tail -20 /var/log/nginx/brp_access.log | grep -c 'upstream=192.168.71.252'   # -> non-zero
```

The `upstream=` prefix matters more here than in step 3. This check passes on a
*non-zero* count, so matching the bare address would let node2's own client
traffic satisfy it while node2 was still drained — a green light to continue,
on the step that returns a node to service.

Let it serve a minute; watch terminal A for errors before proceeding. If the new
version is broken, you find out now, while node1 still runs the old one.

### Step 7 — repeat for node1

Same sequence — drain, confirm zero, update, health-check direct, return to the
pool — but the update command itself is not the same, because node1 runs Docker
Compose rather than a Quadlet:

```bash
ssh ericratz@192.168.71.251      # a new session; leave node2's terminals A and B alone
cd ~/bare-metal-reliability-platform
git pull                       # the image is built from this tree; see preconditions
sed -i 's/^APP_VERSION=.*/APP_VERSION=0.2.0/' .env
# docker-compose.yml reads ${APP_VERSION} for BOTH the build-arg and the runtime
# environment, so the one edit above covers what node2 needed two mechanisms for.
docker compose up -d --build
docker compose ps                # STATUS must reach (healthy), not just Up
```

Then step 5's direct health check against `192.168.71.251:8000`, and
`pool.sh up node1`.

> **VIP interaction.** node1 holds the VIP. `pool.sh down node1` only removes it
> from the *upstream* — it keeps serving as the LB. But when you restart node1's
> **nginx** (not just the app), `check_nginx.sh` fails and the VIP moves to
> node2 for ~3–4s. That is expected. If you are only restarting the app
> container, the VIP does not move. Watch terminal A either way.

### Step 8 — close out the evidence

Ctrl-C terminal A, read the summary (want `failed : 0`, both nodes present in
the by-node breakdown with non-zero counts). Let k6 finish, then:

```bash
echo $?
```

`0` or nothing failed. The script's `handleSummary` **replaces** k6's default
summary, so the `http_req_failed rate==0` threshold verdict is never printed —
the exit code is the only place it surfaces. The `failed : 0` line in the
custom summary asserts the same thing and is derived from the same metric, so
the two must agree; if they ever disagree, trust the exit code and find out why
before quoting either.

Keep the log in `evidence/`.

### If requests were dropped

1. Failure timestamps come from the watch summary.
2. `sudo grep -F "<timestamp>" /var/log/nginx/brp_error.log`

| Symptom | Likely cause |
|---|---|
| 502s at a `pool.sh down` | drained with requests in flight; retry did not cover the gap |
| 502s at a container restart | node restarted while still in the pool — step 3 skipped |
| sustained 504s | `proxy_read_timeout` shorter than the surviving node under full load |
| failures on both nodes | not a rollout problem — new image does not start; check `logs` |

### Rollback

Same procedure, previous `APP_VERSION`; the old image is still on the node.
Roll back the node you just touched **before** proceeding to the other.

---

## 3. VIP failover drills

These prove the HA layer works and, more usefully, *measure* it.

**Run the watcher from node2.** Not because the workstation cannot reach the
VIP — it can, and unlike the direct workstation↔node1 path the VIP stays clean
(keepalived gratuitously ARPs it continuously, so that neighbour entry never
goes stale). node2 is the right choice for a blunter reason: every drill below
takes node1 down in some fashion, and a watcher running on the node you are
about to power off measures nothing.

A second watcher on the workstation is worth running alongside it. It is the
only vantage point that is not one of the two nodes, so it sees the failover
the way an external client does — and during Drill B it is the only watcher
whose own host is not the surviving node.

```bash
# on node2, for the whole of every drill
./scripts/watch-uptime.sh http://192.168.71.245/health
```

Tail the transitions on **both** nodes at once — a failover is two half-events,
one node standing down and the other taking over, and the interesting failures
are the ones where only half happens:

```bash
journalctl -t keepalived-notify -f
```

Timestamps from the two nodes are directly comparable: `notify.sh` now emits an
identical format on both, which it did not before §1e — see `CHANGELOG.md`.

### Drill A — graceful nginx stop on the master

```bash
# on node1 (VIP holder):
sudo systemctl stop nginx
```

Expected: `check_nginx.sh` fails twice (~4s), node1's instance enters FAULT and
resigns with a priority-0 advert, node2 promotes and `notify.sh` logs
`state=MASTER` on node2. Measure dropped requests in terminal A against that
transition timestamp. Restart nginx on node1 — with `nopreempt`, the VIP
**stays** on node2 (no second outage), and node1 returns as BACKUP.

> **This drill failed the first time it was run, on 2026-08-04, and that is
> why the config above says `fall`/`rise` with no `weight`.** The original
> tracking block used `weight -20`, on the reasoning that dropping node1 from
> 110 to 90 puts it under node2's 100. A non-zero weight only adjusts
> priority; it never changes state. node1 stayed MASTER at 90, kept
> advertising, and node2 — which carries `nopreempt` — declined to preempt a
> lower-priority master, because that is precisely what `nopreempt` means.
>
> Result: **57 consecutive dropped requests, zero VRRP transitions on either
> node, and no recovery at all** until nginx was restarted by hand. The VIP sat
> on the one node known to be unable to serve. If you see an unbroken run of
> `code=000` at the VIP with both `journalctl -t keepalived-notify` tails
> silent, that is this bug, not a slow failover — the two are
> indistinguishable from the watcher alone, which is why both tails are part of
> the procedure rather than a nicety.
>
> The measured outage that day was ~12s, and **that number is not a failover
> time.** The `nginx.service` journal showed the first drop 8ms after systemd
> began stopping and the last 31ms before it reported `Started` again, so the
> window is exactly stop-to-manual-restart. Do not quote it. Confirmed with the
> VIP-location check below: `.251` → `1`, `.252` → `0`, and `nopreempt` means
> it cannot have drifted back, so it never left.

### Drill B — hard power loss on the master

Pull power from the VIP holder. Expected: node2 stops hearing adverts, promotes
itself after the master-down interval, VIP moves. Record the gap.

Do **not** assume this is slower than Drill A — the old text here said so, on
the pre-2026-08-04 assumption that Drill A moved the VIP by priority change.
The two are now close enough that the ordering is an open question worth
measuring: Drill A costs `fall × interval` to detect (2–4s) and then resigns
immediately, while Drill B costs `3 × advert_int + (256 − priority)/256`, about
3.6s on node2, with nothing to detect. Drill B may well be the faster of the
two. Whichever it is, it is a measurement, not a prediction.

### Drill C — split brain (cause it once, on purpose)

Block VRRP on one node (`sudo firewall-cmd --remove-protocol=vrrp` on node2,
temporarily). Expected: both nodes claim the VIP; `curl` to it becomes
inconsistent. This teaches you to *recognize* split brain and confirms 1a's
firewall rule is load-bearing. Re-add the rule to recover.

> After any drill, confirm the fleet is back to exactly one VIP holder:
> `for n in 251 252; do echo "node .$n:"; ssh ericratz@192.168.71.$n "ip addr show enp1s0 | grep -c 192.168.71.245"; done`
> — one node returns 1, the other 0.
