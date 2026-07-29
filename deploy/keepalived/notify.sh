#!/usr/bin/env bash
#
# /etc/keepalived/notify.sh
#
# Called by keepalived on every VRRP state transition. Logs to the journal with
# a millisecond timestamp.
#
# This is what makes failover *measurable*. Correlating this against the
# per-request log from scripts/watch-uptime.sh gives you the real number —
# "the VIP moved at 18:42:07.412 and we dropped 3 requests" — instead of a
# vague claim that failover is fast. That number is the point of the drill.
#
#   journalctl -t keepalived-notify -f
#
# Arguments are supplied by keepalived:
#   $1 type  - INSTANCE | GROUP
#   $2 name  - the vrrp_instance name
#   $3 state - MASTER | BACKUP | FAULT | STOP

set -u

TYPE="${1:-?}"
NAME="${2:-?}"
STATE="${3:-?}"

logger -t keepalived-notify \
    "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ) type=${TYPE} instance=${NAME} state=${STATE} host=$(hostname)"
