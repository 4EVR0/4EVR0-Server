#!/bin/bash

# GPU 서버 초기 세팅 스크립트
# Vast.ai 새 인스턴스 빌린 후 실행
# 사용법: ./setup_gpu.sh <TAILSCALE_AUTH_KEY> [HOSTNAME]
#
# HOSTNAME을 고정하면 MagicDNS 주소가 항상 동일하게 유지됨
# 예) ./setup_gpu.sh tskey-xxx vast-gpu-server-2
# 주의: 이전 인스턴스는 Tailscale 관리 콘솔에서 삭제 후 사용 권장
#       https://login.tailscale.com/admin/machines

set -e

TAILSCALE_AUTH_KEY="${1:-}"
TAILSCALE_HOSTNAME="${2:-vast-gpu-server-2}"

if [ -z "$TAILSCALE_AUTH_KEY" ]; then
    echo "오류: Tailscale Auth Key가 필요합니다."
    echo "사용법: $0 <TAILSCALE_AUTH_KEY> [HOSTNAME]"
    echo "  HOSTNAME 기본값: vast-gpu-server-2"
    echo "키 발급: https://login.tailscale.com/admin/settings/keys"
    exit 1
fi

echo "===== [1/5] vLLM 메트릭 설정 ====="

VLLM_CONF="/etc/supervisor/conf.d/vllm.conf"

sed -i 's|^environment=PROC_NAME.*|environment=PROC_NAME="%(program_name)s",VLLM_ARGS="--kv-cache-dtype turboquant_k8v4 --max-num-seqs 8 --enable-auto-tool-choice --tool-call-parser hermes --download-dir /workspace/models --host 0.0.0.0 --port 18000 --enable-metrics"|' $VLLM_CONF

echo "vLLM 설정 완료"

echo "===== [2/5] Caddy /metrics 노출 설정 ====="

CADDYFILE="/etc/Caddyfile"

if grep -q "path /metrics" $CADDYFILE; then
    echo "/metrics 이미 설정됨, 스킵"
else
    sed -i '/path \/portal-resolver/a\\t\t\tpath /metrics' $CADDYFILE
    echo "Caddy /metrics 노출 설정 완료"
fi

echo "===== [3/5] Tailscale 설치 ====="

if command -v tailscale &>/dev/null; then
    echo "Tailscale 이미 설치됨, 스킵"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "Tailscale 설치 완료"
fi

echo "===== [4/5] Tailscale supervisor 등록 ====="

mkdir -p /var/run/tailscale /var/lib/tailscale

cat > /etc/supervisor/conf.d/tailscale.conf << 'EOF'
[program:tailscale]
command=tailscaled --tun=userspace-networking --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock
directory=/var/lib/tailscale
autostart=true
autorestart=true
stderr_logfile=/var/log/portal/tailscale.log
stdout_logfile=/var/log/portal/tailscale.log
priority=10
EOF

echo "supervisor 등록 완료"

echo "===== [5/5] 서비스 재시작 및 Tailscale 인증 ====="

caddy reload --config /etc/Caddyfile

# 기존 tailscaled 프로세스 정리
pkill -f tailscaled 2>/dev/null || true
sleep 1

supervisorctl reread
supervisorctl update
supervisorctl restart vllm

# tailscaled가 뜰 때까지 대기
echo "tailscaled 시작 대기 중..."
for i in $(seq 1 10); do
    if tailscale status &>/dev/null 2>&1; then
        break
    fi
    sleep 2
done

# Tailscale 네트워크 인증 (고정 hostname으로 등록)
tailscale up --auth-key="$TAILSCALE_AUTH_KEY" --hostname="$TAILSCALE_HOSTNAME"
echo "Tailscale 인증 완료"

# Tailscale IP 및 MagicDNS 호스트명 확인
TAILSCALE_IP=$(tailscale ip -4)
TAILSCALE_DOMAIN=$(tailscale status --json 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    suffix = d.get('MagicDNSSuffix', '')
    print(suffix)
except:
    print('')
" 2>/dev/null || echo "")

MAGICDNS_HOST="${TAILSCALE_HOSTNAME}.${TAILSCALE_DOMAIN}"

echo ""
echo "===== 완료 ====="
echo ""
echo "vLLM 로딩 확인:"
echo "  tail -f /var/log/portal/vllm.log"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Tailscale IP:      ${TAILSCALE_IP}"
echo "  MagicDNS 호스트명: ${MAGICDNS_HOST}"
echo ""
echo "  .env에 MagicDNS 호스트명 사용 (IP 변경 불필요):"
echo "  GPU_SERVER_URL=http://${MAGICDNS_HOST}:18000"
echo ""
echo "  ※ 이전 인스턴스가 tailnet에 남아 있으면 호스트명 충돌 가능:"
echo "     https://login.tailscale.com/admin/machines"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
