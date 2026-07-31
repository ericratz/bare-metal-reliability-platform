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

**node1 (Docker):**
```bash
cd ~/bare-metal-reliability-platform
cp .env.example .env          # set NODE_NAME=node1
docker compose up -d --build
curl -s localhost:8000/health | jq '{node, version, status}'
```

**node2 (Podman + Quadlet).** Podman is daemonless, so a plain
`podman run --restart` does not survive reboot — nothing is watching to restart
it. The systemd Quadlet unit is what makes the container a managed service.
Full steps (build, linger, install) are in `deploy/podman/README.md`. In brief:
```bash
cd ~/bare-metal-reliability-platform
cp .env.example .env          # set NODE_NAME=node2
podman build --build-arg APP_VERSION=0.1.0 -t brp-api:latest .
mkdir -p ~/.config/containers/systemd
cp deploy/podman/brp-api.container ~/.config/containers/systemd/
sudo loginctl enable-linger "$USER"   # or the unit dies on logout
systemctl --user daemon-reload && systemctl --user start brp-api
curl -s localhost:8000/health | jq '{node, version, status}'
```

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

```bash
# Debian: /etc/nginx/conf.d/ ; RHEL: same path, both use conf.d
sudo cp deploy/nginx/brp-upstream.conf deploy/nginx/brp.conf /etc/nginx/conf.d/
# target of location = /report — setgid is required, see below the block
sudo install -d -o root -g www-data -m 2750 /var/www/health   # node1 (Ubuntu)
sudo install -d -o root -g nginx    -m 2750 /var/www/health   # node2 (Rocky)
sudo nginx -t && sudo systemctl enable --now nginx
curl -s localhost/nginx-health          # -> "nginx ok"  (nginx itself, never proxied)
curl -s localhost/health | jq '{node, status}'   # proxied to a backend
```

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

```bash
sudo cp deploy/prometheus/prometheus.yml /etc/prometheus/
sudo cp -r deploy/prometheus/rules /etc/prometheus/
# set external_labels.replica to this node's name in prometheus.yml
sudo promtool check config /etc/prometheus/prometheus.yml
sudo systemctl enable --now prometheus
```

Confirm at `http://<node>:9090/targets` — both nodes should show 2 UP `brp-api`
targets and 2 UP `prometheus` targets, from each node's point of view.

**Register.** Add `prometheus.service` to `HEALTH_SERVICES`, and this node's
`http://127.0.0.1:8000/slo` (the `fleet-slo` entry) to `HEALTH_APP_ENDPOINTS`
— not before now. `/slo` reads from local Prometheus and degrades to
`status: unavailable` without it, so registering it any earlier books a
guaranteed failure against the baseline you are trying to keep quiet.

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

```bash
# node1:
sudo cp deploy/keepalived/keepalived-node1.conf /etc/keepalived/keepalived.conf
# node2:
sudo cp deploy/keepalived/keepalived-node2.conf /etc/keepalived/keepalived.conf
# both:
sudo cp deploy/keepalived/check_nginx.sh deploy/keepalived/notify.sh /etc/keepalived/
sudo chmod 755 /etc/keepalived/check_nginx.sh /etc/keepalived/notify.sh
sudo systemctl enable --now keepalived
```

Verify the VIP is up on **exactly one** node (node1 while healthy):
```bash
# node1 — should show 192.168.71.245:
ip addr show enp1s0 | grep 192.168.71.245
# node2 — should show NOTHING:
ip addr show enp1s0 | grep 192.168.71.245
# from anywhere:
curl -s http://192.168.71.245/health | jq '{node, status}'
watch -n1 'journalctl -t keepalived-notify -n3 --no-pager'
```

If the VIP appears on **both** nodes, VRRP adverts are not getting through —
recheck 1a.

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

### Step 0 — start evidence collection (before touching anything)

```bash
# terminal A
./scripts/watch-uptime.sh http://192.168.71.245/health
# terminal B
k6 run -e BASE_URL=http://192.168.71.245 -e DURATION=15m k6/rolling-update.js
```

Both must still be running at the end. If either started late or died, the run
proves nothing — restart from here.

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
sleep 15
sudo tail -20 /var/log/nginx/brp_access.log | grep -c '192.168.71.252'   # -> 0
```

**Do not continue until this is 0.** Restarting a node still taking traffic is
the mistake this whole procedure exists to prevent.

### Step 4 — update node2

```bash
ssh ericratz@192.168.71.252
cd ~/bare-metal-reliability-platform
# rebuild the :latest tag with the new version baked in, restart the unit
podman build --build-arg APP_VERSION=0.2.0 -t brp-api:latest . && systemctl --user restart brp-api
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
sleep 15 && sudo tail -50 /var/log/nginx/brp_access.log | grep -c '192.168.71.252'   # -> non-zero
```

Let it serve a minute; watch terminal A for errors before proceeding. If the new
version is broken, you find out now, while node1 still runs the old one.

### Step 7 — repeat for node1

Same sequence, `pool.sh down node1` … update … `pool.sh up node1`.

> **VIP interaction.** node1 holds the VIP. `pool.sh down node1` only removes it
> from the *upstream* — it keeps serving as the LB. But when you restart node1's
> **nginx** (not just the app), `check_nginx.sh` fails and the VIP moves to
> node2 for ~3–4s. That is expected. If you are only restarting the app
> container, the VIP does not move. Watch terminal A either way.

### Step 8 — close out the evidence

Ctrl-C terminal A, read the summary (want `failed : 0`, both nodes present in
the by-node breakdown). Let k6 finish; it must report `http_req_failed
rate==0`. Keep the log in `evidence/`.

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

These prove the HA layer works and, more usefully, *measure* it. Run
`./scripts/watch-uptime.sh http://192.168.71.245/health` throughout each, and
tail the transitions:

```bash
journalctl -t keepalived-notify -f
```

### Drill A — graceful nginx stop on the master

```bash
# on node1 (VIP holder):
sudo systemctl stop nginx
```

Expected: `check_nginx.sh` fails twice (~4s), node1's priority drops below
node2's, VIP moves to node2, `notify.sh` logs `state=MASTER` on node2. Measure
dropped requests in terminal A against the transition timestamp. Restart nginx
on node1 — with `nopreempt`, the VIP **stays** on node2 (no second outage).

### Drill B — hard power loss on the master

Pull power from the VIP holder. Expected: node2 stops hearing adverts, promotes
itself after ~3–4s, VIP moves. This is the ungraceful path — slower than Drill A
because it waits for advert timeout rather than a priority change. Record the
gap.

### Drill C — split brain (cause it once, on purpose)

Block VRRP on one node (`sudo firewall-cmd --remove-protocol=vrrp` on node2,
temporarily). Expected: both nodes claim the VIP; `curl` to it becomes
inconsistent. This teaches you to *recognize* split brain and confirms 1a's
firewall rule is load-bearing. Re-add the rule to recover.

> After any drill, confirm the fleet is back to exactly one VIP holder:
> `for n in 251 252; do echo "node .$n:"; ssh ericratz@192.168.71.$n "ip addr show enp1s0 | grep -c 192.168.71.245"; done`
> — one node returns 1, the other 0.
