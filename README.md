# 4EVR0-Server

# MLOps 모니터링 파이프라인 운영 가이드

## 전체 구조

```
[Vast.ai GPU 서버]          [AWS EC2 t3.small]
  vLLM (port 18000)  ──────▶  Prometheus (9090)
  Caddy (port 8000)           Grafana (3000)
  /metrics 노출
```

---

## 처음 EC2 세팅 (최초 1회만)

### 1. EC2 인스턴스 생성
- AMI: Ubuntu 24.04 LTS
- 인스턴스 유형: t3.small
- 보안 그룹 인바운드: 22, 3000, 9090 포트 오픈

### 2. Docker 설치
```bash
sudo apt update
sudo apt install -y docker.io docker-compose
sudo usermod -aG docker $USER
newgrp docker
```

### 3. 모니터링 스택 실행
```bash
mkdir ~/monitoring && cd ~/monitoring

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

docker-compose up -d
```

### 4. Grafana 설정
1. `http://EC2_IP:3000` 접속 (ID: admin / PW: admin)
2. Connections → Data sources → Add data source → Prometheus
3. URL: `http://prometheus:9090` 입력 후 Save & Test

---

## 새 GPU 서버 빌릴 때마다 하는 것

### Step 1. Vast.ai 인스턴스 생성 후 SSH 접속
```bash
ssh -p <포트> root@<IP>
```

### Step 2. 프로젝트 클론
```bash
cd /workspace
git clone <레포주소>
```

### Step 3. GPU 서버 세팅 스크립트 실행
```bash
# setup_gpu.sh를 GPU 서버로 복사하거나, 직접 붙여넣기
chmod +x setup_gpu.sh
./setup_gpu.sh
```

vLLM 로딩 완료 확인:
```bash
tail -f /var/log/portal/vllm.log
# "Application startup complete" 뜨면 완료
```

메트릭 노출 확인:
```bash
curl http://localhost:18000/metrics
```

### Step 4. Vast.ai 대시보드에서 포트 확인
- 인스턴스 → 🔑 아이콘 클릭
- Open Ports에서 8000번 포트에 매핑된 외부 포트 확인
- 예시: `74.15.83.230:49852 -> 8000/tcp` → IP: 74.15.83.230, 포트: 49852

### Step 5. EC2에서 Prometheus 타겟 업데이트
```bash
chmod +x update_prometheus.sh
./update_prometheus.sh <GPU서버_IP> <포트>

# 예시
./update_prometheus.sh 74.15.83.230 49852
```

### Step 6. 연결 확인
- Prometheus: `http://EC2_IP:9090/targets` → State: UP 확인
- Grafana: `http://EC2_IP:3000` → Explore에서 `vllm:num_requests_running` 조회

---

## GPU 서버 종료할 때

Prometheus가 scrape 실패해도 EC2는 정상 동작해요.  
새 인스턴스 빌리면 Step 1~6만 다시 하면 돼요.

---

## 주요 접속 정보

| 서비스 | URL |
|--------|-----|
| Grafana | `http://EC2_IP:3000` |
| Prometheus | `http://EC2_IP:9090` |
| vLLM API | `http://GPU_IP:외부포트/v1` |
| vLLM 메트릭 | `http://GPU_IP:외부포트/metrics` |

---

## 핵심 Grafana 쿼리

```promql
# 처리 중인 요청 수
vllm:num_requests_running

# 대기 중인 요청 수 (병목 감지)
vllm:num_requests_waiting

# 초당 생성 토큰 수
rate(vllm:generation_tokens_total[1m])

# P99 응답 시간
histogram_quantile(0.99, rate(vllm:e2e_request_latency_seconds_bucket[5m]))

# GPU KV Cache 사용률
vllm:gpu_cache_usage_perc
```