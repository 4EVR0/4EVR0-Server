# 4EVR0-Server

화장품 성분·제품 추천 API 서버. 사용자 자연어 입력 → GPU 서버(vLLM) 프로필 추출 → Neo4j 성분·제품 쿼리 → 추천 응답 생성. 단일 정적 웹 UI(챗봇)도 같은 서버가 함께 서빙한다.

---

## 동작 흐름

```
[사용자] ──자연어 고민──▶ [앱 서버 · FastAPI]
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        [GPU 서버]       [Neo4j 서버]      [PostgreSQL / Redis]
        Vast.ai · vLLM    AWS EC2           세션·대화 저장
        (Qwen3.5-9B)      Graph DB
              │               │
   ① 프로필 추출       ② 효능→성분 조회
   (자연어→JSON,        ③ 성분→제품 조회
    실패 시 규칙 폴백)
              │               │
              └───────┬───────┘
                      ▼
       ④ LLM 추천 응답 생성 (성분 설명 → 제품 추천, 한글 성분명 + 근거 수준 인용)
                      ▼
                  [사용자]
```

모든 서버는 **Tailscale VPN + MagicDNS**로 연결된다. `.env`에서 IP 대신 MagicDNS 호스트명을 쓰므로 인스턴스를 교체해도 설정 수정이 불필요하다.

| 서버 | MagicDNS 호스트명 | 역할 |
|------|------------------|------|
| 앱 서버 (Mac) | `macbook-pro-3.tailb70036.ts.net` | FastAPI + 웹 UI |
| GPU 서버 (Vast.ai) | `vast-gpu-server-2.tailb70036.ts.net:18000` | vLLM (`Qwen3.5-9B` AWQ int4) |
| Neo4j 서버 (EC2) | `ip-172-31-56-102.tailb70036.ts.net:7687` | Graph DB |
| 모니터링 서버 (EC2) | `monitoring-server-1.tailb70036.ts.net` | Prometheus + Grafana |

---

## 성능·품질 엔지니어링 (측정 기반 최적화)

> 단일 GPU(RTX 3090) · `Qwen3.5-9B` · vLLM 서빙에서 **측정으로 병목을 찾고 → 레버를 걸고 →
> 결과를 품질 게이트로 검증**하는 LLMOps 루프. 상세 근거·원자료는 `review/` 문서.

### 1. 서빙 지연(latency) 최적화 — decode가 병목
단건 latency를 span별로 분해하니 **generate/decode가 총 시간의 76~87%**. 병목을 정조준:

| 레버 | 효과 | 성격 |
|---|---|---|
| **응답 스트리밍(SSE)** | 체감 TTFT **10s → 2.7s (~4.3×)** | 구조 데이터 즉시 + 토큰 스트림 |
| **간결 프롬프트(v6)** | total **−30%** (12s → 8.35s), 품질 유지(judge OVERALL 4.52→4.46, grounding 동일) | 출력 토큰↓ |
| **동시성 제어(세마포어+429)** | 과부하 시 실패율 **8.7% → 0%** | admission control(붕괴 방지) |
| **프리픽스 캐싱** | 처리량 **+12%**, p95 **−11%** | vLLM 엔진 튜닝 |

부하 테스트로 **처리량 천장 ≈ 0.76 RPS**(단일 8B GPU 한계)를 확인 → 근본 해결은 서빙 레버(양자화)로.

### 2. 양자화 (AWQ int4) — 비용↓ 하되 품질 게이트로 검증 ⭐
`Qwen3.5-9B` bf16 → AWQ int4 A/B. **"빠르게 만들되 품질 회귀를 eval로 막는다"** 규율로 채택 결정:

| 지표 | bf16 | AWQ int4 | Δ |
|---|---|---|---|
| decode 속도(batch1) | 46 tok/s | **111 tok/s** | **2.4×** |
| 동시 처리량(8스트림) | 254 tok/s | **572 tok/s** | **2.25×** |
| 가중치 VRAM | 17.7GB | 5.3GB | −70% |
| 최대 동시성 @32K ctx | ~2.3× | **10.3×** | KV 캐시 4.4× |
| **품질 게이트(judge OVERALL)** | 4.46 | **4.58** (grounding 유지) | ✅ 통과 |

→ **채택.** 트레이드오프는 TTFT +19%(전체에 묻힘)와 추출 precision 소폭 하락(recall은 상승)뿐.

### 3. 콜드스타트 — 진단하고 앱·인프라 양면에서 해결 (실측 검증) ⭐
신규 GPU 대여 시 서버 준비까지의 비용을 실측 분해하고, 두 축으로 해결·검증:

- **앱 사이드(구현·검증):** readiness 게이트(콜드 vLLM 라우팅 차단) + startup 워밍업 + 커넥션 풀링 + 캐시 single-flight.
  - 워밍업 A/B(콜드 compile): 첫 요청 컴파일 꼬리 **+4.42s → +1.25s** (워밍업 더미가 대신 지불, **−72%**).
- **인프라(영구 볼륨) — 실측 검증됨:** `/workspace`에 영구 볼륨 마운트 → 같은 볼륨 재부팅 A/B:

  | 단계 | 1차(빈 볼륨) | 2차(볼륨 유지) | 효과 |
  |---|---|---|---|
  | 가중치 다운로드 | 30.1s | **0s** | ✅ 볼륨이 재다운로드 제거 |
  | torch.compile | 45.9s | **12.6s** | ✅ compile 캐시 재사용(−73%) |
  | 서버 준비 총 | ~165s | **~110s** | −33% |

  > (측정: RTX 3090, AWQ int4 + HF_TOKEN. 원 진단의 "다운로드 216s"는 bf16+무토큰 기준 — 채택 구성에선
  > 다운로드가 애초에 30s이고 볼륨이 이를 0으로 만든다. 남는 최대 비용 = 매 부팅 cudagraph/KV 캡처 ~94s.)

### 4. 품질 회귀 게이트 (eval-in-CI) ⭐
프롬프트·모델·검색 변경이 품질을 떨어뜨리면 **CI가 자동 차단**. 유닛테스트가 "버그=머지 금지"라면 이건 "**품질 저하=머지 금지**":

- **오프라인 eval 하네스:** 추출 정확도(concern F1·skin_type accuracy) + 생성 품질(LLM-as-judge — OVERALL·grounding·format, 외부 `gpt-4o-mini`, bootstrap 95% CI).
- **게이트:** 결과를 임계값과 비교 → 미달 시 CI 실패 + PR 코멘트 점수 표(self-hosted 러너 + 라벨 트리거).
- **검증:** 채택 AWQ 통과 / 실제 회귀 사례(프롬프트 v5, grounding **4.55→3.80**)를 **차단** 확인.

> **방법론 — judge 게이트 규율:** 모든 채택 결정(양자화·프롬프트)은 "느낌"이 아니라
> *judge OVERALL이 baseline CI 안 + grounding 무회귀*라는 동일 규율로 판정하고, 이를 §4에서 CI로 자동화했다.

---

## 웹 UI

별도 프론트엔드 레포·빌드 단계 없이, 앱 서버가 단일 정적 파일을 직접 서빙한다.

- 파일: `app/static/index.html` (HTML/CSS/JS 한 파일)
- 경로: `GET /` → `index.html` 반환, `/static`에 정적 마운트
- 특징: 연한 초록 챗봇 UI, 마크다운 렌더링, 성분 카드에 `한글명(영어명)` + 근거 tier 표시
- 호출 API: 같은 서버의 `POST /api/v1/sessions`, `POST /api/v1/recommend`

---

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET`  | `/` | 웹 UI(챗봇) |
| `GET`  | `/health` | 의존성(Neo4j/PostgreSQL/Redis) 상태 |
| `GET`  | `/docs` | Swagger UI |
| `GET`  | `/metrics` | Prometheus 메트릭 |
| `POST` | `/api/v1/sessions` | 세션 생성 |
| `POST` | `/api/v1/recommend` | 추천 (성분 + 제품 + 자연어 응답) |
| `GET`  | `/api/v1/recommend/path` | 효능→성분→제품 그래프 경로 조회 |
| `POST` | `/api/v1/profile/extract` | 자연어 → 피부 프로필 추출 |

---

## 프로젝트 구조

```
app/
  main.py              # FastAPI 진입점 (라우터·미들웨어·정적 서빙)
  api/                 # 라우터: health, sessions, profile, recommend
  services/            # 비즈니스 로직 (recommend, 프로필 추출, taxonomy 정규화)
  clients/             # 외부 연동 (vLLM/LLM, Neo4j, 폴백)
  repositories/        # 대화·세션 저장 (PostgreSQL)
  prompts/             # 버전 관리되는 프롬프트 (*.txt + 로더)
  schemas/ domain/     # Pydantic 스키마, 도메인 enum
  core/                # 설정, 로깅, 메트릭, 예외 처리, 미들웨어
  static/index.html    # 웹 UI
eval/                  # 프로필 추출 / 응답 품질(LLM-judge) 평가
tests/                 # pytest 단위 테스트
```

프롬프트는 코드에 하드코딩하지 않고 `app/prompts/*.txt`로 분리·버전 관리한다. 추천 응답 프롬프트는 현재 `recommend_response.v4`가 프로덕션 기본이다.

---

## 환경 변수 (`.env`)

```env
APP_NAME=4EVR0 Cosmetic Recommendation API
APP_VERSION=1.0.0
DEBUG=false

POSTGRES_DSN=postgresql://cosmetic_user:cosmetic_pass@postgresql:5432/cosmetic_db
REDIS_URL=redis://redis:6379

NEO4J_URI=bolt://ip-172-31-56-102.tailb70036.ts.net:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<비밀번호>

GPU_SERVER_URL=http://vast-gpu-server-2.tailb70036.ts.net:18000
GPU_MODEL=Qwen/Qwen3.5-9B          # vLLM이 실제 서빙하는 모델명과 반드시 일치
GPU_TIMEOUT_SECONDS=60

GEN_TEMPERATURE=0.3                # 추천 응답 생성 온도 (eval 재현 시 0)
GEN_MAX_TOKENS=1200                # 응답 잘림 방지용 출력 여유
```

> ⚠️ `GPU_MODEL`이 vLLM 실제 서빙 모델과 불일치하면 404 → 규칙 기반 폴백으로 동작한다.

샘플은 `.env.example` 참고.

---

## 실행

### Docker (운영)
```bash
docker compose up
```
앱(8000) + PostgreSQL + Neo4j + Redis + promtail 컨테이너가 함께 뜬다.

### 로컬 개발 (hot reload)
맥 로컬 PostgreSQL이 5432를 점유하면 Docker PostgreSQL을 5433으로 우회:
```bash
docker run -d --name 4evr0-postgresql \
  -e POSTGRES_USER=cosmetic_user -e POSTGRES_PASSWORD=cosmetic_pass \
  -e POSTGRES_DB=cosmetic_db -p 5433:5432 postgres:16
docker compose up -d redis

POSTGRES_DSN="postgresql://cosmetic_user:cosmetic_pass@localhost:5433/cosmetic_db" \
REDIS_URL="redis://localhost:6379" \
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- 웹 UI: `http://localhost:8000/`
- API 문서: `http://localhost:8000/docs`
- 헬스체크: `http://localhost:8000/health`

> 정적 UI(`index.html`)는 요청마다 디스크에서 읽으므로 재시작 없이 새로고침으로 반영된다. 프롬프트·서비스·설정 변경은 서버 재시작이 필요하다(`--reload` 미사용 시).

---

## 테스트 & 평가

```bash
# 단위 테스트
pytest tests/

# 프로필 추출 평가 (라벨 50건)
python eval/run_eval.py --no-mlflow

# 추천 응답 품질 평가 (외부 LLM judge 필요 — 생성기와 다른 모델)
JUDGE_MODEL=<external-model> JUDGE_API_KEY=<key> \
  python eval/run_response_eval.py --gen-temperature 0

# 품질 회귀 게이트 (임계 미달 시 exit 1 → CI 머지 차단)
python eval/check_gate.py --extraction <run_eval.json> --response <run_response_eval.json>
```

응답 평가는 자기 채점을 거부하며(외부 judge 강제), grounding·conciseness 등 5개 축을 1~5점으로 채점한다.
품질 게이트는 PR에 `run-eval` 라벨을 붙이면 self-hosted 러너에서 자동 실행된다
(`.github/workflows/eval-gate.yml` — 프롬프트·모델·검색 변경의 품질 회귀를 머지 전 차단).
자세한 내용은 `eval/README.md`, `.github/workflows/README-eval-gate.md` 참고.

---

## 인프라 (별도 레포)

| 대상 | 레포 |
|------|------|
| GPU(vLLM) 서버 프로비저닝 (`setup_tailscale.sh` + vast.ai 템플릿) | [`GPU_Serving_Infra`](https://github.com/4EVR0/GPU_Serving_Infra) |
| 모니터링 스택 (Prometheus/Grafana/Loki) | [`Monitoring_Infra`](https://github.com/4EVR0/Monitoring_Infra) |

GPU 인스턴스를 같은 호스트명(`vast-gpu-server-2`)으로 다시 등록하면 MagicDNS 주소가 유지되어 `.env` 수정이 불필요하다.

### 모니터링

| 서비스 | URL |
|--------|-----|
| Grafana | `http://monitoring-server-1.tailb70036.ts.net:3000` |
| Prometheus | `http://monitoring-server-1.tailb70036.ts.net:9090` |
| vLLM 메트릭 | `http://vast-gpu-server-2.tailb70036.ts.net:18000/metrics` |

핵심 Grafana 쿼리:
```promql
vllm:num_requests_running                                                    # 처리 중 요청
vllm:num_requests_waiting                                                    # 대기 요청 (병목 감지)
rate(vllm:generation_tokens_total[1m])                                       # 초당 생성 토큰
histogram_quantile(0.99, rate(vllm:e2e_request_latency_seconds_bucket[5m]))  # P99 응답시간
vllm:gpu_cache_usage_perc                                                    # GPU KV Cache 사용률
```
