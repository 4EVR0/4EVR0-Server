#!/bin/bash

# EC2 Prometheus IP 업데이트 스크립트
# 새 Vast.ai GPU 서버 빌린 후 EC2에서 실행
# 사용법: ./update_prometheus.sh <새_GPU서버_IP> <포트>
# 예시:   ./update_prometheus.sh 74.15.83.230 49852

set -e

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "사용법: $0 <GPU서버_IP> <포트>"
    echo "예시:   $0 74.15.83.230 49852"
    exit 1
fi

GPU_IP=$1
GPU_PORT=$2
MONITORING_DIR="$HOME/monitoring"

echo "===== Prometheus 타겟 업데이트 ====="
echo "GPU 서버: $GPU_IP:$GPU_PORT"

cat > $MONITORING_DIR/prometheus.yml << EOF
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'vllm'
    static_configs:
      - targets: ['$GPU_IP:$GPU_PORT']
    metrics_path: '/metrics'
EOF

echo "prometheus.yml 업데이트 완료"

echo "===== Prometheus 재시작 ====="
cd $MONITORING_DIR
docker-compose restart prometheus

echo ""
echo "===== 완료 ====="
echo "Prometheus 타겟 확인:"
echo "  http://$(curl -s ifconfig.me):9090/targets"