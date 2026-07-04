#!/usr/bin/env bash
# 콜드스타트 측정용 내구성 SSH 터널 (이슈 #36).
#   - caffeinate -s 로 측정 내내 맥 절전 차단 (2026-07-03 ON 런 실패 원인 = 절전으로 터널 끊김)
#   - ServerAliveInterval 로 유휴 끊김 방지 + 드롭 시 자동 재접속
# 사용: ./load/coldstart_tunnel.sh <tailscale-host> [localport=18010]
#   예: ./load/coldstart_tunnel.sh vast-gpu-server-2.tailb70036.ts.net
set -euo pipefail
HOST="${1:?usage: coldstart_tunnel.sh <tailscale-host> [localport]}"
LPORT="${2:-18010}"

echo "[tunnel] $HOST 127.0.0.1:18000 -> localhost:$LPORT (caffeinate + auto-reconnect)"
exec caffeinate -s bash -c '
while true; do
  ssh -N \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
      -L '"$LPORT"':127.0.0.1:18000 root@'"$HOST"' || true
  echo "[tunnel] dropped at $(date +%H:%M:%S) — 3s 후 재접속" >&2
  sleep 3
done'
