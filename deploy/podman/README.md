# node2 (Podman) deployment

node1 runs the app via Docker Compose (`docker-compose.yml` at the repo root).
node2 runs the **same image** via a Podman Quadlet systemd unit — the RHEL-native,
daemonless equivalent. Same image, different launcher; that split is deliberate
(see the top-level README).

## First-time install

```bash
# 1. build the image (build-arg bakes the version /health reports)
cd ~/bare-metal-reliability-platform
cp .env.example .env          # set NODE_NAME=node2
podman build --build-arg APP_VERSION=0.1.0 -t brp-api:latest .

# 2. install the Quadlet unit
mkdir -p ~/.config/containers/systemd
cp deploy/podman/brp-api.container ~/.config/containers/systemd/

# 3. keep user services running after logout (see note in the unit file)
sudo loginctl enable-linger "$USER"

# 4. start it
systemctl --user daemon-reload
systemctl --user start brp-api
systemctl --user status brp-api --no-pager

# 5. verify
curl -s localhost:8000/health | jq '{node, version, status}'
```

## Updating (rolling-update step 4 on node2)

The Quadlet references a stable `:latest` tag; the *version* is baked in at
build time via the build-arg and reported by `/health`. So a deploy is a rebuild
of that tag plus a restart — no edit to the unit file:

```bash
podman build --build-arg APP_VERSION=0.2.0 -t brp-api:latest .
systemctl --user restart brp-api
curl -s localhost:8000/health | jq .version   # -> "0.2.0"
```

## SELinux

If the container is denied at startup (port bind, volume, etc.), inspect the
actual denial rather than disabling SELinux:

```bash
sudo ausearch -m avc -ts recent
```

Add the specific policy the AVC names and record it as a node2-only step.
Blanket-disabling SELinux discards the exact Debian-vs-RHEL portability signal
this project exists to surface.
