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

# %N and truncate here, NOT %3N. Ubuntu 26.04 ships the Rust coreutils, whose
# `date` ignores the width modifier and emits nine digits; GNU date on Rocky
# honours it and emits three. The pair therefore logged two different timestamp
# formats, which makes correlating a failover ACROSS the two nodes manual work
# — and correlating the two nodes is the entire reason this file exists.
# Truncating in bash gives byte-identical output from either implementation.
TS="$(date -u +%Y-%m-%dT%H:%M:%S.%N)"
TS="${TS:0:23}Z"

# $HOSTNAME is a bash builtin and costs no exec. $(hostname) was used here and
# returned EMPTY on node2: under SELinux, keepalived_t is denied even getattr
# on hostname_exec_t, so the child process cannot run it. Confirmed by AVC, not
# inferred — the binary is installed and works fine from a normal shell.
#
# The failure was silent in the worst way: the line still logged, still parsed,
# and merely lost the field identifying which node emitted it.
HOST="${HOSTNAME:-unknown}"

logger -t keepalived-notify \
    "${TS} type=${TYPE} instance=${NAME} state=${STATE} host=${HOST}"
