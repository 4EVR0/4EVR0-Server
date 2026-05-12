#!/bin/bash

# GPU 서버 초기 세팅 스크립트
# Vast.ai 새 인스턴스 빌린 후 실행

set -e

echo "===== [1/3] vLLM 메트릭 설정 ====="

cat > /etc/vllm-args.conf << 'EOF'
--compilation-config '{"cudagraph_capture_sizes": [1,2,3,4,5,6,7,8]}'
EOF

# supervisor 설정에 VLLM_ARGS 적용
VLLM_CONF="/etc/supervisor/conf.d/vllm.conf"

# 기존 environment 줄 교체
sed -i 's|^environment=PROC_NAME.*|environment=PROC_NAME="%(program_name)s",VLLM_ARGS="--kv-cache-dtype turboquant_k8v4 --max-num-seqs 8 --enable-auto-tool-choice --tool-call-parser hermes --download-dir /workspace/models --host 0.0.0.0 --port 18000 --enable-metrics"|' $VLLM_CONF

echo "vLLM 설정 완료"

echo "===== [2/3] Caddy /metrics 노출 설정 ====="

CADDYFILE="/etc/Caddyfile"

# /metrics 경로가 이미 있으면 추가 안 함
if grep -q "path /metrics" $CADDYFILE; then
    echo "/metrics 이미 설정됨, 스킵"
else
    # path /portal-resolver 다음 줄에 /metrics 추가
    sed -i '/path \/portal-resolver/a\\t\t\tpath /metrics' $CADDYFILE
    echo "Caddy /metrics 노출 설정 완료"
fi

echo "===== [3/3] 서비스 재시작 ====="

caddy reload --config /etc/Caddyfile
supervisorctl reread
supervisorctl restart vllm

echo ""
echo "===== 완료 ====="
echo "vLLM 로딩 중... 아래 명령어로 완료 확인:"
echo "  tail -f /var/log/portal/vllm.log"
echo ""
echo "완료 후 메트릭 확인:"
echo "  curl http://localhost:18000/metrics"