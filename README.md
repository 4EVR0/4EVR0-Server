# 4EVR0-Server

화장품 성분 및 제품 추천 API 서버. 사용자 자연어 입력 → GPU 서버(vLLM) 프로필 추출 → Neo4j 성분·제품 쿼리 → 추천 응답 생성.

---

## 전체 아키텍처

```
[사용자]
   │
   ▼
[앱 서버 - FastAPI]
   │
   ├──────────────────────────┐
   ▼                          ▼
[GPU 서버]              [Neo4j 서버]
 Vast.ai                  AWS EC2
 vLLM (Qwen3.5-9B)     Graph DB
   │                          │
   │  ① 피부 프로필 추출       │  ② 성분 조회 (effects → ingredients)
   │  (자연어 → JSON)          │  ③ 제품 조회 (ingredients → products)
   │                          │
   └──────────────────────────┘
   │
   ▼  ④ 추천 응답 생성 (성분 + 제품 + 자연어)
[사용자]

※ 모든 서버는 Tailscale VPN + MagicDNS로 연결
  (인스턴스 교체 시 .env 수정 불필요)
```

### 서버별 Tailscale 정보

| 서버 | MagicDNS 호스트명 | Tailscale IP | 역할 |
|------|-----------------|-------------|------|
| 앱 서버 (Mac) | `macbook-pro-3.tailb70036.ts.net` | `100.114.44.9` | FastAPI |
| GPU 서버 (Vast.ai) | `vast-gpu-server-2.tailb70036.ts.net` | `100.100.75.44` | vLLM (Qwen3.5-9B) |
| Neo4j 서버 (EC2) | `ip-172-31-56-102.tailb70036.ts.net` | `100.72.139.8` | Graph DB |
| 모니터링 서버 (EC2) | `monitoring-server.tailb70036.ts.net` | - | Prometheus + Grafana |

> IP 대신 **MagicDNS 호스트명**을 사용하면 인스턴스를 교체해도 `.env` 수정이 불필요.

---

## 최초 1회 세팅

### 1. Mac에 Tailscale 설치
[tailscale.com/download](https://tailscale.com/download) 에서 Mac 앱 설치 후 계정 로그인.

### 2. Neo4j 서버 Tailscale 등록
Neo4j 서버는 AWS EC2에서 상시 실행 중. 최초 1회만 Tailscale 등록 필요.

### 3. 모니터링 서버 세팅 (EC2 t3.small)

모니터링 스택(Prometheus/Grafana/Loki) 프로비저닝과 대시보드는 별도 repo로 분리됨.
→ **[`Monitoring_Infra`](https://github.com/4EVR0/Monitoring_Infra)** 참고.

---

## GPU 서버 새로 빌릴 때 (destroy → 재생성)

GPU(vLLM) 추론 서버 프로비저닝은 별도 repo로 분리됨.
→ **[`GPU_Serving_Infra`](https://github.com/4EVR0/GPU_Serving_Infra)** 의 `setup_gpu.sh` + README 참고.

요약:
```bash
# Vast.ai vLLM 인스턴스 SSH 접속 후 (전체 clone 불필요, 스크립트만)
curl -fsSL https://raw.githubusercontent.com/4EVR0/GPU_Serving_Infra/main/setup_gpu.sh \
  | bash -s <TAILSCALE_AUTH_KEY> vast-gpu-server-2
tail -f /var/log/portal/vllm.log   # "Application startup complete" 대기
```
> 같은 호스트명(`vast-gpu-server-2`)으로 등록하면 MagicDNS 주소가 유지되어 아래 `.env` 수정 불필요.
> ⚠️ `.env`의 `GPU_MODEL` 이 vLLM 실제 서빙 모델과 **일치**해야 함(불일치 시 404 → 규칙기반 폴백).

Prometheus 스크레이프 타겟 갱신이 필요하면 `Monitoring_Infra`의 `prometheus.yml` 에서 처리.

---

## 앱 서버 .env 구성

```env
APP_NAME=4EVR0 Cosmetic Recommendation API
APP_VERSION=1.0.0
DEBUG=false

POSTGRES_DSN=postgresql://cosmetic_user:cosmetic_pass@postgresql:5432/cosmetic_db

NEO4J_URI=bolt://ip-172-31-56-102.tailb70036.ts.net:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<비밀번호>

REDIS_URL=redis://redis:6379

GPU_SERVER_URL=http://vast-gpu-server-2.tailb70036.ts.net:18000
GPU_MODEL=Qwen/Qwen3.5-9B
GPU_TIMEOUT_SECONDS=60
```

---

## 앱 서버 실행

### Docker (운영 환경)
```bash
docker compose up
```

### 로컬 개발 환경
맥에 로컬 PostgreSQL이 5432를 점유한 경우 Docker PostgreSQL을 5433으로 우회:
```bash
# 의존성 컨테이너만 실행
docker run -d --name 4evr0-postgresql \
  -e POSTGRES_USER=cosmetic_user \
  -e POSTGRES_PASSWORD=cosmetic_pass \
  -e POSTGRES_DB=cosmetic_db \
  -p 5433:5432 postgres:16

docker compose up -d redis

# 앱 직접 실행 (hot reload)
POSTGRES_DSN="postgresql://cosmetic_user:cosmetic_pass@localhost:5433/cosmetic_db" \
REDIS_URL="redis://localhost:6379" \
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

API 문서: `http://localhost:8000/docs`  
헬스체크: `http://localhost:8000/health`

---

## 모니터링

| 서비스 | URL |
|--------|-----|
| Grafana | `http://monitoring-server.tailb70036.ts.net:3000` (admin/admin) |
| Prometheus | `http://monitoring-server.tailb70036.ts.net:9090` |
| vLLM API | `http://vast-gpu-server-2.tailb70036.ts.net:18000/v1` |
| vLLM 메트릭 | `http://vast-gpu-server-2.tailb70036.ts.net:18000/metrics` |

### 핵심 Grafana 쿼리

```promql
vllm:num_requests_running          # 처리 중인 요청 수
vllm:num_requests_waiting          # 대기 중인 요청 수 (병목 감지)
rate(vllm:generation_tokens_total[1m])                                    # 초당 생성 토큰
histogram_quantile(0.99, rate(vllm:e2e_request_latency_seconds_bucket[5m]))  # P99 응답시간
vllm:gpu_cache_usage_perc          # GPU KV Cache 사용률
```
