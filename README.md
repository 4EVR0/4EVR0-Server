# 4EVR0-Server

화장품 성분 추천 API 서버. 사용자 자연어 입력 → GPU 서버(vLLM) 프로필 추출 → Neo4j 성분 쿼리 → 추천 응답 생성.

---

## 전체 아키텍처

```
[사용자]
   │
   ▼
[앱 서버 - FastAPI]
   │              │
   ▼              ▼
[GPU 서버]     [Neo4j 서버]
 Vast.ai         AWS EC2
 vLLM            Graph DB
   │
   ▼
[모니터링]
 AWS EC2
 Prometheus + Grafana

※ GPU 서버 ↔ Neo4j 서버 ↔ 앱 서버는 Tailscale VPN으로 연결
  (IP 변경 시에도 Tailscale IP 사용으로 .env 수정 불필요)
```

### 서버별 Tailscale IP

| 서버 | Tailscale IP | 역할 |
|---|---|---|
| 앱 서버 (Mac) | `100.114.44.9` | FastAPI |
| GPU 서버 (Vast.ai) | `100.117.78.10` | vLLM (Qwen3-8B-FP8) |
| Neo4j 서버 (EC2) | `100.72.139.8` | Graph DB |

---

## 최초 1회 세팅

### 1. Mac에 Tailscale 설치
[tailscale.com/download](https://tailscale.com/download) 에서 Mac 앱 설치 후 계정 로그인.

### 2. Neo4j 서버 Tailscale 등록
Neo4j 서버는 AWS EC2에서 상시 실행 중. 최초 1회만 Tailscale 등록 필요.

### 3. 모니터링 스택 세팅 (EC2 t3.small)
```bash
sudo apt update && sudo apt install -y docker.io docker-compose
mkdir ~/monitoring && cd ~/monitoring

cat > docker-compose.yml << 'EOF'
version: '3'
services:
  prometheus:
    image: prom/prometheus
    ports: ["9090:9090"]
    volumes: [./prometheus.yml:/etc/prometheus/prometheus.yml]
    restart: always
  grafana:
    image: grafana/grafana
    ports: ["3000:3000"]
    environment: [GF_SECURITY_ADMIN_PASSWORD=admin]
    restart: always
EOF

docker-compose up -d
```

---

## GPU 서버 새로 빌릴 때 (destroy → 재생성)

### Step 1. Vast.ai 인스턴스 생성 후 SSH 접속
```bash
ssh -p <포트> root@<IP>
```

### Step 2. 레포 클론 후 세팅 스크립트 실행
```bash
cd /workspace
git clone https://github.com/4EVR0/4EVR0-Server.git
bash 4EVR0-Server/setup_gpu.sh <TAILSCALE_AUTH_KEY>
```

> Auth Key 발급: [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys) → Generate auth key (Reusable 체크)

또는 git clone 없이 바로 실행:
```bash
curl -fsSL https://raw.githubusercontent.com/4EVR0/4EVR0-Server/main/setup_gpu.sh | bash -s <TAILSCALE_AUTH_KEY>
```

### Step 3. vLLM 로딩 확인
```bash
tail -f /var/log/portal/vllm.log
# "Application startup complete" 뜨면 완료 (모델 최초 다운로드 시 수분 소요)
```

### Step 4. .env 업데이트 (Tailscale IP 바뀐 경우에만)
스크립트 마지막 출력에서 확인:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Tailscale IP: 100.x.x.x
  .env 업데이트 필요:
  GPU_SERVER_URL=http://100.x.x.x:8000
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Step 5. Prometheus 타겟 업데이트 (Tailscale IP 바뀐 경우에만)
```bash
./update_prometheus.sh <GPU_TAILSCALE_IP> 8000
```

---

## 앱 서버 .env 구성

```env
POSTGRES_DSN=postgresql://cosmetic_user:cosmetic_pass@postgresql:5432/cosmetic_db

NEO4J_URI=bolt://100.72.139.8:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<비밀번호>

REDIS_URL=redis://redis:6379

GPU_SERVER_URL=http://100.117.78.10:8000
GPU_MODEL=Qwen/Qwen3-8B-FP8
GPU_TIMEOUT_SECONDS=60
GPU_AUTH_TOKEN=<Caddy Bearer 토큰>
```

---

## 앱 서버 실행

```bash
docker compose up
```

---

## 모니터링

| 서비스 | URL |
|---|---|
| Grafana | `http://<EC2_IP>:3000` (admin/admin) |
| Prometheus | `http://<EC2_IP>:9090` |
| vLLM API | `http://100.117.78.10:8000/v1` |
| vLLM 메트릭 | `http://100.117.78.10:8000/metrics` |

### 핵심 Grafana 쿼리

```promql
vllm:num_requests_running          # 처리 중인 요청 수
vllm:num_requests_waiting          # 대기 중인 요청 수 (병목 감지)
rate(vllm:generation_tokens_total[1m])                                    # 초당 생성 토큰
histogram_quantile(0.99, rate(vllm:e2e_request_latency_seconds_bucket[5m]))  # P99 응답시간
vllm:gpu_cache_usage_perc          # GPU KV Cache 사용률
```
