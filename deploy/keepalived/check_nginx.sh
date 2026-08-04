#!/usr/bin/env bash
#
# /etc/keepalived/check_nginx.sh
#
# keepalived vrrp_script health check. Exit 0 = this node's Nginx is serving;
# non-zero = it is not, and after `fall` consecutive failures keepalived puts
# this node's VRRP instance into FAULT, which resigns the VIP to the peer.
#
# FAULT, not a priority adjustment. The tracking block deliberately carries no
# `weight` — see keepalived-node1.conf. With a weight this check ran correctly,
# reported failure correctly, and still could not move the VIP, because a
# priority change is not a state change. A health check is only as good as what
# it is wired into.
#
# Deliberately hits /nginx-health, which Nginx answers itself and never
# proxies. Two failure modes this avoids:
#
#   pgrep nginx    - the classic mistake. A wedged Nginx still has a process,
#                    so the VIP stays on a node that cannot serve. That is HA
#                    theatre: the check passes precisely when you need it to
#                    fail.
#
#   curl /health   - proxied to the app tier. If both app instances are down,
#                    this fails on BOTH nodes, and VRRP flaps the VIP back and
#                    forth between two equally-broken load balancers. Moving
#                    the VIP cannot fix an app-tier outage, so it should not
#                    try.
#
# Install: root-owned, mode 755. keepalived's enable_script_security refuses to
# execute scripts writable by non-root.

exec curl -sf -m 2 -o /dev/null http://127.0.0.1/nginx-health
