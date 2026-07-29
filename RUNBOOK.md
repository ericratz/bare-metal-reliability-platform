# Runbook

Operational procedures for the two-node platform:

1. [First-time deployment](#1-first-time-deployment) — per-node, OS-specific
2. [Zero-downtime rolling update](#2-zero-downtime-rolling-update)
3. [VIP failover drills](#3-vip-failover-drills)

All client-facing traffic and all testing go at the **VIP `192.168.71.250`**,
never a node IP — hitting a node directly bypasses the thing under test.

Nodes: **node1** `192.168.71.251` (Ubuntu 26.04, Docker), **node2**
`192.168.71.252` (Rocky 10.2, Podman). keepalived priorities: node1 110, node2
100, so node1 holds the VIP when both are healthy.

---

## 1. First-time deployment

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
```bash
sudo ufw allow 80/tcp
sudo ufw allow from 192.168.68.0/22 to any port 8000 proto tcp
sudo ufw allow from 192.168.68.0/22 to any port 9090 proto tcp
# VRRP is not a TCP/UDP port — allow the protocol to the peer
sudo ufw allow from 192.168.71.252 proto vrrp
sudo ufw --force enable && sudo ufw status verbose
```

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

### 1c. nginx (both nodes)

```bash
# Debian: /etc/nginx/conf.d/ ; RHEL: same path, both use conf.d
sudo cp deploy/nginx/brp-upstream.conf deploy/nginx/brp.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl enable --now nginx
curl -s localhost/nginx-health          # -> "nginx ok"
curl -s localhost/health | jq '{node, status}'   # proxied to a backend
```

Run the proxied `curl` a few times on each node — `node` should vary between
node1 and node2, confirming the upstream reaches both.

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

### 1e. keepalived (both nodes)

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
# node1 — should show 192.168.71.250:
ip addr show enp1s0 | grep 192.168.71.250
# node2 — should show NOTHING:
ip addr show enp1s0 | grep 192.168.71.250
# from anywhere:
curl -s http://192.168.71.250/health | jq '{node, status}'
watch -n1 'journalctl -t keepalived-notify -n3 --no-pager'
```

If the VIP appears on **both** nodes, VRRP adverts are not getting through —
recheck 1a.

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
./scripts/watch-uptime.sh http://192.168.71.250/health
# terminal B
k6 run -e BASE_URL=http://192.168.71.250 -e DURATION=15m k6/rolling-update.js
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
`./scripts/watch-uptime.sh http://192.168.71.250/health` throughout each, and
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
> `for n in 251 252; do echo "node .$n:"; ssh ericratz@192.168.71.$n "ip addr show enp1s0 | grep -c 192.168.71.250"; done`
> — one node returns 1, the other 0.
