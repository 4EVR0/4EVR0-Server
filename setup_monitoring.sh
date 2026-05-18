#!/bin/bash

# 모니터링 서버 초기 세팅 스크립트 (EC2 t3.small 등)
# - Tailscale 설치 및 등록 (MagicDNS hostname 고정)
# - Prometheus + Grafana Docker Compose 세팅
# - Prometheus가 MagicDNS로 GPU 서버 메트릭 스크레이핑
#
# 사용법: bash setup_monitoring.sh <TAILSCALE_AUTH_KEY> [HOSTNAME]
# 예시:   bash setup_monitoring.sh tskey-auth-xxx monitoring-server

set -e

TAILSCALE_AUTH_KEY="${1:-}"
TAILSCALE_HOSTNAME="${2:-monitoring-server}"
MONITORING_DIR="$HOME/monitoring"
TAILNET="tailb70036.ts.net"
GPU_HOST="vast-gpu-server-2.${TAILNET}"
GPU_METRICS_PORT=18000

if [ -z "$TAILSCALE_AUTH_KEY" ]; then
    echo "사용법: $0 <TAILSCALE_AUTH_KEY> [HOSTNAME]"
    echo "Auth Key 발급: https://login.tailscale.com/admin/settings/keys (Reusable 체크)"
    exit 1
fi

echo "===== [1/4] Tailscale 설치 ====="

if command -v tailscale &>/dev/null; then
    echo "Tailscale 이미 설치됨, 스킵"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "Tailscale 설치 완료"
fi

echo "===== [2/4] Tailscale 등록 (hostname: ${TAILSCALE_HOSTNAME}) ====="

tailscale up --auth-key="$TAILSCALE_AUTH_KEY" --hostname="$TAILSCALE_HOSTNAME"

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "확인 필요")
MAGICDNS_HOST="${TAILSCALE_HOSTNAME}.${TAILNET}"

echo "Tailscale 등록 완료"
echo "  IP: ${TAILSCALE_IP}"
echo "  MagicDNS: ${MAGICDNS_HOST}"

echo "===== [3/4] Docker 설치 확인 ====="

if ! command -v docker &>/dev/null; then
    sudo apt-get update -q
    sudo apt-get install -y docker.io docker-compose
    sudo usermod -aG docker "$USER"
    echo "Docker 설치 완료"
else
    echo "Docker 이미 설치됨, 스킵"
fi

echo "===== [4/4] Prometheus + Grafana 세팅 ====="

mkdir -p "$MONITORING_DIR"
cd "$MONITORING_DIR"

cat > docker-compose.yml << 'EOF'
version: '3'
services:
  prometheus:
    image: prom/prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    restart: always
  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    restart: always
EOF

# GPU 서버를 MagicDNS 호스트명으로 스크레이핑
# GPU 서버가 꺼져있어도 Prometheus는 정상 기동, 재접속을 자동으로 재시도함
cat > prometheus.yml << EOF
global:
  scrape_interval: 15s
  scrape_timeout: 10s

scrape_configs:
  - job_name: 'vllm'
    static_configs:
      - targets: ['${GPU_HOST}:${GPU_METRICS_PORT}']
    metrics_path: '/metrics'
EOF

docker-compose up -d

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  모니터링 서버 세팅 완료"
echo ""
echo "  Tailscale IP:      ${TAILSCALE_IP}"
echo "  MagicDNS 호스트명: ${MAGICDNS_HOST}"
echo ""
echo "  Grafana:    http://${TAILSCALE_IP}:3000  (admin / admin)"
echo "  Prometheus: http://${TAILSCALE_IP}:9090"
echo ""
echo "  GPU 메트릭 타겟: ${GPU_HOST}:${GPU_METRICS_PORT}/metrics"
echo "  → GPU 서버를 같은 hostname으로 재등록하면 자동 재연결됨"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
