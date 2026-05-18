#!/bin/bash

# EC2 Prometheus 타겟 업데이트 스크립트
#
# [MagicDNS 사용 시 - 권장]
# GPU 서버를 같은 hostname으로 재등록했다면 이 스크립트 실행 불필요.
# prometheus.yml에 MagicDNS 호스트명이 이미 고정되어 있음.
#
# [수동 IP 지정 시]
# 사용법: ./update_prometheus.sh <GPU_IP> <포트>
# 예시:   ./update_prometheus.sh 100.100.75.44 18000   (Tailscale IP)
#          ./update_prometheus.sh 74.15.83.230 49852    (공개 IP, Caddy 포트)

set -e

MONITORING_DIR="$HOME/monitoring"
TAILNET="tailb70036.ts.net"
GPU_HOST="vast-gpu-server-2.${TAILNET}"
GPU_PORT=18000

if [ -n "$1" ] && [ -n "$2" ]; then
    # 수동 지정 모드
    GPU_HOST=$1
    GPU_PORT=$2
    echo "===== Prometheus 타겟 수동 업데이트 ====="
    echo "타겟: $GPU_HOST:$GPU_PORT"
else
    # MagicDNS 기본값으로 리셋
    echo "===== Prometheus 타겟을 MagicDNS 기본값으로 업데이트 ====="
    echo "타겟: $GPU_HOST:$GPU_PORT"
fi

cat > $MONITORING_DIR/prometheus.yml << EOF
global:
  scrape_interval: 15s
  scrape_timeout: 10s

scrape_configs:
  - job_name: 'vllm'
    static_configs:
      - targets: ['${GPU_HOST}:${GPU_PORT}']
    metrics_path: '/metrics'
EOF

echo "prometheus.yml 업데이트 완료"

echo "===== Prometheus 재시작 ====="
cd $MONITORING_DIR
docker-compose restart prometheus

echo ""
echo "===== 완료 ====="
echo "Prometheus 타겟 확인: http://$(tailscale ip -4 2>/dev/null || curl -s ifconfig.me):9090/targets"
