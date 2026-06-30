# Latency 최적화 — 실험 계획 (2026-07-01)

> 부하 테스트 프로젝트(`review/2026-06-30-load-test.md`)에 이어, **단건 응답 latency**를 줄이는
> 별도 챕터. 부하/동시성이 아니라 **"한 요청이 왜 10~30s 걸리고, 무엇으로 줄이나"** 에 답한다.
>
> 이 문서는 **재개용 마스터 플랜**이다. 중단 후 다시 와도 8절(재개 가이드)부터 보면 이어서 진행 가능.

---

## 0. 배경·목표

- **현 상태:** 추천 1건이 `extract(LLM) → neo4j → generate(LLM)` 순차. 부하 테스트에서 생성
  단계가 단건 ~10s, 부하 시 ~26~30s로 측정됨. 사용자는 그 시간 동안 **빈 화면**을 본다.
- **목표:** 단건 latency(특히 **체감 latency**)를 줄인다. 동시에, 효과가 **없는** 레버도
  실제로 시도·측정해 **"왜 안 통했는지"를 데이터로** 남긴다(포트폴리오: 가정 기각이 아니라 실증).
- **핵심 주제 문장:** "여러 latency 레버를 체계적으로 적용·측정했다. 스트리밍과 출력 길이 축소는
  효과가 컸고, 프롬프트 길이 축소·KV/프리픽스 캐싱·검색 최적화는 **우리 워크로드에선 효과가 작았으며
  그 이유를 측정값으로 설명한다**(아래 1절 가설을 1·2절 측정으로 검증)."

## 1. 핵심 모델 — prefill vs decode (검증할 가설)

LLM 추론은 두 단계이고, 단건 latency는 한쪽이 지배한다(가설 → 측정으로 증명).

| 단계 | 하는 일 | 성격 | 무엇이 건드리나 |
|------|---------|------|----------------|
| **Prefill** | 입력 토큰(시스템+사용자+성분) 처리 | **병렬** 1회 forward | 프롬프트 길이, KV/프리픽스 캐싱 |
| **Decode** | 출력 토큰을 **하나씩** 생성 | **순차** 토큰당 forward | 출력 토큰 수, 토큰당 속도(FP8) |

- **가설:** Prefill은 전체 latency의 소수(~몇 %)이고 Decode가 지배한다.
- **함의(검증 대상):** 프롬프트 길이↓·KV/프리픽스 캐싱은 **Prefill만** 건드리므로 단건 효과가 작다.
  반대로 **출력 토큰↓·FP8**(Decode)과 **스트리밍**(체감)이 진짜 레버다.
- **증명 방법:** 스트리밍으로 얻는 **TTFT**로 latency를 prefill/decode로 분해한다(2절).

## 2. 측정 지표 정의

| 지표 | 정의 | 무엇을 말하나 |
|------|------|--------------|
| **TTFT** | Time To First Token — 요청부터 첫 생성 토큰까지 | 체감 latency. 스트리밍이 줄이는 것 |
| **TPOT** | Time Per Output Token = decode시간 / (출력토큰−1) | 디코드 속도. FP8가 줄이는 것 |
| **Total E2E** | 요청부터 완료까지 | 전체 |
| **단계별** | extract / neo4j / generate | 서버 `/metrics`(`recommend_stage_latency_seconds`)에서 |
| **출력 토큰 수** | 생성된 토큰 수 | vLLM `request_generation_tokens` |

**prefill/decode 분해 (스트리밍 TTFT 활용):**
```
Total      = extract + neo4j + generate(prefill + decode)
TTFT       ≈ extract + neo4j + generate_prefill + (1st token)
→ generate_prefill ≈ TTFT − extract − neo4j   (extract·neo4j는 서버 stage 메트릭에서)
→ decode           ≈ Total − TTFT
→ prefill 비중(%)  = generate_prefill / Total   ← 1절 가설의 직접 증거
```

### 2.1 요청당 latency 트레이스 스키마 (우리 파이프라인에 재단)
일반 RAG 템플릿(`embedding/rerank/...`)이 아니라 **실제 코드 경로**에 맞춘 span. 추적 가치 기준 =
"그걸로 *행동*을 바꾸느냐". 존재하지 않거나(µs) 무의미한 span(normalize/prompt_build/postprocess)은
한 칸 `overhead_ms`로 묶어 **"~0임을 한 번 측정해 증명"** 하고 잊는다.

```
cache_lookup_ms      # recommend_cache.get (Redis). hit이면 이 뒤 전부 스킵
extract_ms           # LLM #1 (짧은 JSON 추출)
retrieval_ms         # neo4j 그래프 조회
gate_wait_ms         # 세마포어 큐 대기 (extract+generate 슬롯 확보 대기 합). 부하 시만 의미
generate_ttft_ms     # 생성 첫 토큰까지 (P1 스트리밍에서 분리; baseline은 generate 한 덩어리)
generate_decode_ms   # 생성 디코드 꼬리 (= generate − ttft)
overhead_ms          # normalize+prompt_build+postprocess 합산(~0 확인용 1칸)
total_ms
```
- **제거:** `embedding_ms`(시맨틱 캐시 v2 가야 부활), `rerank_ms`(리랭커 없음) — 둘 다 현재 0.
- **추가:** `extract_ms`(LLM이 2번이라 generate와 분리), `gate_wait_ms`(부하 때 extract에 섞여
  보였던 큐 대기를 분리).
- **구현:** Prometheus `recommend_latency_span_seconds{span}` 히스토그램 + `trace_id` 구조 로그
  1줄/요청(기존 Loki 관측 인프라에 그대로 실림 — 추가 인프라 0).
- **단계 의존:** `generate_ttft/decode` 분리는 P1(스트리밍) 이후. baseline(P0)은 `generate_ms` 한 덩어리.

## 3. 측정 방법론

- **단건 저부하 반복**(부하 테스트 아님). 같은/다른 메시지를 순차로 N회 → 분포(p50/p90).
- **변수 하나씩**(load test 규율 동일). 레버 1개 적용 → before/after 기록.
- **캐시 주의:** 응답 캐시가 켜져 있으면 히트가 GPU를 가린다 → latency 실측 시 **신규(nonce) 메시지**로
  미스 강제 또는 `RECOMMEND_CACHE_ENABLED=false`.
- **워밍업:** GPU 콜드스타트 1건 제외 후 측정.
- 도구: `load/latency_bench.py`(신설) — TTFT/TPOT/total/단계별 분해 출력.

## 4. 실험 매트릭스 (전체 레버 — 다 시도)

| # | 레버 | 가설 | 측정 | 예상(검증 대상) | 측 |
|---|------|------|------|----------------|----|
| 0 | **baseline** | — | TTFT·total·단계·prefill/decode 분해 | 기준선 | — |
| 1 | **스트리밍(SSE)** | TTFT 급감 | TTFT, total | TTFT 10~30s→~수초, total= | app |
| 2 | **프롬프트 길이↓** | prefill↓ | TTFT/total/prefill | **효과 작음 → 이유(prefill 비중)** | app |
| 3 | **KV/프리픽스 캐싱** | prefill 재사용 | TTFT(프리픽스 hit vs cold) | **단건 효과 작음 → 이유** | 서버(이미 ON) |
| 4 | **출력 토큰↓**(간결 프롬프트·구조 제한·max_tokens) | decode↓ | total, 출력토큰수 | total 의미있게↓ (품질 게이트) | app |
| 5 | **extract 경량화**(guided JSON/작은 모델) | extract↓ | extract 단계 | 소~중 | app/서버 |
| 6 | **검색(neo4j)** | 이미 ~0.1s | neo4j 단계 | **비병목 확인**(완성도) | — |

- **양성 예상:** 1(TTFT), 4(total).
- **음성 예상(중요 — 실증):** 2, 3, 6 → "시도했으나 효과 작음 + 측정된 이유"로 문서화.
- **품질 게이트:** 4(출력 토큰↓)와 5(extract 변경)는 출력이 바뀌므로 `eval/`(judge)로 품질 회귀 확인 후 채택.

## 5. 스트리밍 설계 (레버 #1, 측정 도구 겸용)

**목표:** 구조 데이터(성분·제품) 즉시 + 생성 텍스트 토큰 스트림 → TTFT를 ~수초로.

### 5.1 엔드포인트
- **신설** `POST /api/v1/recommend/stream` (SSE). 기존 `POST /api/v1/recommend`는 유지
  (일괄 응답·캐시히트·하위호환).

### 5.2 SSE 프로토콜 (`text/event-stream`)
```
event: meta
data: {"session_id","turn_id","ingredients":[...],"products":[...],"model_used"}   // neo4j 직후 즉시

event: delta
data: {"text":"<토큰 청크>"}        // vLLM stream=True 청크마다 (반복)

event: done
data: {"finish_reason":"stop"}

event: error
data: {"error_code","message"}
```

### 5.3 서비스 흐름
1. 캐시 조회 → **히트:** `meta` + 전체 텍스트 1청크 + `done` (스트림 불필요).
2. **미스:** extract → neo4j → `meta` emit → `llm_slot()` 안에서 `chat.completions.create(stream=True)`
   async 이터레이션 → `delta` emit + 토큰 누적 → 완료 시 `recommend_cache.set`(누적 텍스트) → `done`.
3. **게이트(reject 모드):** 빈 슬롯 없으면 **스트림 시작 전** 429(이미 200 헤더 나가면 못 바꿈).
   → 라우트에서 슬롯 확보/거절을 `StreamingResponse` 구성 전에 판단. 큐 모드는 첫 토큰 전 대기.
4. **폴백:** 생성 중 오류 → 누적분까지 보낸 뒤 `error`(또는 템플릿 폴백 텍스트 1청크) — 단, 폴백은 캐시 안 함.

### 5.4 메트릭
- 신설 `recommend_ttft_seconds`(Histogram) — 첫 토큰까지.
- 기존 `recommend_stage_latency_seconds` 유지.

### 5.5 측정 하네스 (`load/latency_bench.py`)
- 비스트리밍/스트리밍 모두 측정. 스트리밍은 첫 `delta` 수신 시각 = TTFT, `done` 시각 = total.
- 출력: TTFT·total·단계별·prefill/decode 분해(2절 공식) p50/p90.

## 6. 구현 단계 (phase gates)

- [ ] **P0. 측정 하네스 + baseline** — `load/latency_bench.py`, 비스트리밍 단건 분해 baseline 기록.
- [ ] **P1. 스트리밍** — 엔드포인트/서비스/메트릭. TTFT 측정 → baseline 대비.
- [ ] **P2. 음성 레버 실증** — 프롬프트 길이↓(2), 프리픽스 hit vs cold 단건(3), neo4j 확인(6).
      각 측정 + "효과 작음 + 이유" 기록.
- [ ] **P3. 양성 레버** — 출력 토큰↓(4): 프롬프트로 간결화 → total·출력토큰 측정 + **eval 품질 게이트**.
- [ ] **P4. extract 경량화(5)** — guided JSON 등 → extract 단계 측정 (+ 품질 게이트).
- [ ] **P5. 종합 문서화** — `review/2026-07-01-latency-optimization.md`에 baseline→각 레버 before/after,
      양성/음성 결과, prefill/decode 분해로 음성 이유 증명.

각 단계 후 멈춰 결과 공유(phase gate).

## 7. 범위 밖 (Out of scope)

- **FP8 양자화:** decode(TPOT) 레버이나 **eval 주도(품질 회귀)** + 서빙 인프라(`GPU_Serving_Infra`)
  → 별도 워크스트림. 이 챕터에선 "decode를 줄이는 또 다른 축"으로만 참조.
- **스트리밍 프런트엔드 렌더링**: 백엔드 SSE까지. UI 소비는 별도.
- **레플리카/수평 확장**: throughput 영역(부하 테스트 챕터 6절).

## 8. 재개 가이드 (중단 후 여기부터)

### 현재 상태 (2026-07-01)
- main: 부하 테스트 완료(세마포어·응답 캐시·프리픽스 캐싱 머지, PR #29~33).
- vLLM **프리픽스 캐싱 ON**(GPU 서버). 응답 캐시 기본 ON, 세마포어 N=8 기본.
- 이 챕터 브랜치: **`perf/latency-optimization`** (main에서 분기 예정).

### 환경 기동 (로컬)
```bash
# 의존성 컨테이너
docker run -d --name 4evr0-postgresql -e POSTGRES_USER=cosmetic_user \
  -e POSTGRES_PASSWORD=cosmetic_pass -e POSTGRES_DB=cosmetic_db -p 5432:5432 postgres:16
docker run -d --name 4evr0-redis -p 6379:6379 redis:7

# 앱 (로컬 DB override; latency 실측 시 캐시 끄려면 RECOMMEND_CACHE_ENABLED=false)
POSTGRES_DSN="postgresql://cosmetic_user:cosmetic_pass@localhost:5432/cosmetic_db" \
REDIS_URL="redis://localhost:6379" LOG_FORMAT=plain \
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
- GPU(vLLM, Tailscale)·Neo4j(원격)는 상시. `curl .../v1/models`로 도달 확인.
- ⚠️ GPU는 Vast.ai라 측정 세션 동안만 켜두면 비용 절약.

### 다음 액션
1. `git checkout -b perf/latency-optimization main`
2. P0: `load/latency_bench.py` 작성 → baseline 측정.
3. 이후 6절 phase gate 순서대로.

### 관련 파일
- 생성 호출: `app/services/recommend_service.py::_build_llm_response`
- 추출 호출: `app/clients/llm_client.py::call_llm`
- 게이트: `app/clients/llm_gate.py` / 캐시: `app/repositories/recommend_cache.py`
- 라우트: `app/api/recommend.py` / 메트릭: `app/core/metrics.py` / 설정: `app/core/config.py`
