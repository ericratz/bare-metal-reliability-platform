# node2 (Podman) deployment

node1 runs the app via Docker Compose (`docker-compose.yml` at the repo root).
node2 runs the **same image** via a Podman Quadlet systemd unit — the RHEL-native,
daemonless equivalent. Same image, different launcher; that split is deliberate
(see the top-level README).

## First-time install

```bash
# 1. build the image
#    --format docker: OCI has no healthcheck field, so the default format
#    DROPS the Dockerfile HEALTHCHECK with only a build warning. Docker on
#    node1 keeps it, so without this flag the two nodes run measurably
#    different artifacts from the same Dockerfile.
#    APP_VERSION must match .env — see step 2's note; on this node .env wins.
cd ~/bare-metal-reliability-platform
cp .env.example .env          # set NODE_NAME=node2 and APP_VERSION=0.1.0
podman build --format docker --build-arg APP_VERSION=0.1.0 -t brp-api:latest .

# 2. install the Quadlet unit
mkdir -p ~/.config/containers/systemd
cp deploy/podman/brp-api.container ~/.config/containers/systemd/

# 3. keep user services running after logout (see note in the unit file)
sudo loginctl enable-linger "$USER"

# 4. start it
systemctl --user daemon-reload
systemctl --user start brp-api
systemctl --user status brp-api --no-pager

# 5. verify — 127.0.0.1, never `localhost`, see the pasta note in the unit
curl -s http://127.0.0.1:8000/health | jq '{node, version, status}'
podman ps --format '{{.Names}}\t{{.Status}}'   # Status must show (healthy)
pgrep -a pasta | grep -- --ipv4-only          # the v6-reset fix, on the process
ss -ltn | grep 8000                            # 0.0.0.0:8000, never *:8000
```

## Updating (rolling-update step 4 on node2)

The Quadlet references a stable `:latest` tag, so a deploy is a rebuild of that
tag plus a restart — no edit to the unit file.

**Bump `.env` as well as the build-arg.** The unit's `EnvironmentFile` injects
`APP_VERSION` at runtime, and that shadows the value the build-arg baked into
the image. Rebuild alone and `/health` keeps reporting the old version, which
would fail RUNBOOK.md §2 step 5 — or worse, pass it while the check is watching
a value the rebuild never touched:

```bash
sed -i 's/^APP_VERSION=.*/APP_VERSION=0.2.0/' .env
podman build --format docker --build-arg APP_VERSION=0.2.0 -t brp-api:latest .
systemctl --user restart brp-api
curl -s http://127.0.0.1:8000/health | jq .version   # -> "0.2.0"
```

node1 needs no equivalent step: compose derives the build-arg, the image tag and
the runtime env from the one `.env` value.

## SELinux

If the container is denied at startup (port bind, volume, etc.), inspect the
actual denial rather than disabling SELinux:

```bash
sudo ausearch -m avc -ts recent
```

Add the specific policy the AVC names and record it as a node2-only step.
Blanket-disabling SELinux discards the exact Debian-vs-RHEL portability signal
this project exists to surface.
