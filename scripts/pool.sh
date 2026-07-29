#!/usr/bin/env bash
#
# Take a node out of, or put it back into, the Nginx upstream pool.
#
#   sudo ./scripts/pool.sh status
#   sudo ./scripts/pool.sh down node2
#   sudo ./scripts/pool.sh up   node2
#
# Runs on node1 (the only node running Nginx).
#
# This exists because draining a node by hand is the step in the rolling update
# most likely to go wrong under pressure — editing the live upstream file in vi
# and reloading without testing first is how a maintenance window turns into an
# outage. Every change here is syntax-checked before it is applied, and the
# original file is backed up.
#
# Deliberately NOT a full rolling-update script. The procedure in RUNBOOK.md is
# meant to be performed and understood step by step; this only automates the
# one operation that is fiddly and dangerous to fumble.

set -euo pipefail

CONF="${CONF:-/etc/nginx/conf.d/brp-upstream.conf}"
MARKER="#DOWN "

usage() {
    echo "usage: $0 {status|down|up} [node1|node2]" >&2
    exit 2
}

require_conf() {
    if [[ ! -f "$CONF" ]]; then
        echo "error: $CONF not found. Is this node1, and is the config deployed?" >&2
        exit 1
    fi
}

# Reload only if the config parses. `nginx -s reload` on a broken config leaves
# the old workers running but means the change you think you made did not land —
# worse than failing loudly, because you proceed believing the node is drained.
safe_reload() {
    if ! nginx -t; then
        echo "error: nginx config test failed — NOT reloading. Restoring backup." >&2
        cp -a "$CONF.bak" "$CONF"
        exit 1
    fi
    nginx -s reload
    echo "nginx reloaded"
}

show_status() {
    require_conf
    echo "upstream members in $CONF:"
    grep -E '^\s*(#DOWN\s+)?server\s' "$CONF" | while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*#DOWN ]]; then
            printf '  [OUT] %s\n' "$(echo "$line" | sed 's/^[[:space:]]*#DOWN[[:space:]]*//')"
        else
            printf '  [IN ] %s\n' "$(echo "$line" | sed 's/^[[:space:]]*//')"
        fi
    done
}

set_state() {
    local action="$1" node="$2"
    require_conf

    if ! grep -qE "^\s*(#DOWN\s+)?server\s.*#${node}\s*$" "$CONF"; then
        echo "error: no upstream line tagged #${node} in $CONF" >&2
        exit 1
    fi

    cp -a "$CONF" "$CONF.bak"

    case "$action" in
        down)
            if grep -qE "^\s*#DOWN\s+server\s.*#${node}\s*$" "$CONF"; then
                echo "$node is already out of the pool"
                exit 0
            fi
            sed -i -E "s|^(\s*)(server\s.*#${node}\s*)$|\1${MARKER}\2|" "$CONF"
            echo "$node removed from pool"
            ;;
        up)
            if ! grep -qE "^\s*#DOWN\s+server\s.*#${node}\s*$" "$CONF"; then
                echo "$node is already in the pool"
                exit 0
            fi
            sed -i -E "s|^(\s*)#DOWN\s+(server\s.*#${node}\s*)$|\1\2|" "$CONF"
            echo "$node returned to pool"
            ;;
    esac

    # Guard against draining everything. An empty upstream makes Nginx fail its
    # config test, so this would be caught anyway — but the message there is
    # cryptic, and at 2am a clear one is worth the four lines.
    if ! grep -qE '^\s*server\s' "$CONF"; then
        echo "error: that would empty the upstream pool — refusing. Restoring." >&2
        cp -a "$CONF.bak" "$CONF"
        exit 1
    fi

    safe_reload
    echo
    show_status
}

[[ $# -ge 1 ]] || usage

case "$1" in
    status) show_status ;;
    down|up)
        [[ $# -eq 2 ]] || usage
        set_state "$1" "$2"
        ;;
    *) usage ;;
esac
