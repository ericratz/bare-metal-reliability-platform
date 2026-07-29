#!/usr/bin/env bash
#
# Continuous request loop against the load balancer. This is the evidence
# artifact for the zero-downtime claim: it runs for the whole rolling update
# and logs the outcome of every single request with a timestamp and the node
# that served it.
#
# k6 gives you the statistics; this gives you the raw per-request trail you can
# actually point at when someone asks "how do you know none were dropped?" —
# including the exact wall-clock second of any failure, so it can be lined up
# against the runbook step you were performing.
#
#   ./scripts/watch-uptime.sh                          # defaults to localhost
#   ./scripts/watch-uptime.sh http://192.168.71.250/health
#   INTERVAL=0.1 ./scripts/watch-uptime.sh http://192.168.71.250/health
#
# Ctrl-C to stop and print the summary.
#
# Requires bash 4+ (associative arrays). Present on both Ubuntu 26.04 and
# Rocky 10 by default.

set -uo pipefail

URL="${1:-http://localhost/health}"
INTERVAL="${INTERVAL:-0.2}"
TIMEOUT="${TIMEOUT:-5}"
OUT="${OUT:-evidence/uptime-$(date -u +%Y%m%dT%H%M%SZ).log}"

mkdir -p "$(dirname "$OUT")"

total=0
ok=0
fail=0
declare -A per_node=()
declare -A per_code=()
first_fail=""
last_fail=""

BODY=$(mktemp)
running=1
trap 'running=0' INT TERM
trap 'rm -f "$BODY"' EXIT

unattributed=0

printf '# watching %s every %ss\n' "$URL" "$INTERVAL" | tee "$OUT"
printf '# %-24s %-6s %-10s %s\n' "timestamp" "code" "seconds" "node" | tee -a "$OUT"

while (( running )); do
    ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)

    # Body goes to a file so stdout carries ONLY the -w metadata. Parsing both
    # out of one stream with tail/cut misreads any response whose body line
    # structure varies, and silently writes a duration into the status column.
    # -w always yields a code; a refused connection or timeout gives 000, which
    # is exactly the case a naive `curl -f` loop would skip over entirely.
    meta=$(curl -s -m "$TIMEOUT" -o "$BODY" -w '%{http_code} %{time_total}' "$URL" 2>/dev/null)
    code="${meta%% *}"
    secs="${meta##* }"

    [[ -z "$code" ]] && code="000"
    [[ -z "$secs" ]] && secs="0"

    # Tolerate whitespace after the colon. Compact JSON (what the app emits)
    # and pretty-printed JSON both parse, so attribution does not quietly turn
    # into "-" if anything upstream reformats the body.
    node=$(grep -o '"node"[[:space:]]*:[[:space:]]*"[^"]*"' "$BODY" \
        | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    if [[ -z "$node" ]]; then
        node="-"
        [[ "$code" == "200" ]] && unattributed=$((unattributed + 1))
    fi

    total=$((total + 1))
    per_code["$code"]=$(( ${per_code["$code"]:-0} + 1 ))

    if [[ "$code" == "200" ]]; then
        ok=$((ok + 1))
        per_node["$node"]=$(( ${per_node["$node"]:-0} + 1 ))
    else
        fail=$((fail + 1))
        [[ -z "$first_fail" ]] && first_fail="$ts"
        last_fail="$ts"
        # surfaced immediately: you want to see this the second it happens,
        # while you still know which runbook step you are on
        printf '\n!! FAILURE %s code=%s\n' "$ts" "$code" >&2
    fi

    printf '%-26s %-6s %-10s %s\n' "$ts" "$code" "$secs" "$node" >> "$OUT"

    # heartbeat so a long quiet run visibly differs from a hung one
    if (( total % 50 == 0 )); then
        printf 'ok=%d fail=%d\n' "$ok" "$fail"
    fi

    sleep "$INTERVAL"
done

echo
echo "=== uptime watch summary ==="
echo "  url            : $URL"
echo "  total requests : $total"
echo "  succeeded      : $ok"
echo "  failed         : $fail"

if (( total > 0 )); then
    pct=$(awk -v o="$ok" -v t="$total" 'BEGIN{printf "%.4f", (o/t)*100}')
    echo "  success rate   : ${pct}%"
fi

echo
echo "  by node:"
for n in "${!per_node[@]}"; do
    printf '    %-10s %d\n' "$n" "${per_node[$n]}"
done

# Silent loss of attribution would make the evidence log look complete while
# being unable to show traffic actually moved between nodes — say so loudly.
if (( unattributed > 0 )); then
    echo
    echo "  WARNING: $unattributed successful response(s) carried no node field."
    echo "  Attribution is incomplete. Check the URL points at /health and that"
    echo "  NODE_NAME is set on both nodes."
fi

echo
echo "  by status code:"
for c in "${!per_code[@]}"; do
    printf '    %-10s %d\n' "$c" "${per_code[$c]}"
done

if (( fail > 0 )); then
    echo
    echo "  first failure  : $first_fail"
    echo "  last failure   : $last_fail"
    echo
    echo "  Zero-downtime NOT demonstrated. Cross-reference those timestamps"
    echo "  against /var/log/nginx/brp_error.log to see which upstream dropped."
else
    echo
    echo "  Zero dropped requests."
fi

echo
echo "  full log: $OUT"

# non-zero exit when downtime was observed, so this can gate a CI job or a
# scripted runbook without anyone having to read the output
(( fail == 0 ))
