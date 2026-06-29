# 부하 테스트 (Load Testing)

추천 API에 **동시 요청을 쏟아부어** GPU(vLLM) 병목이 어떻게 드러나는지 측정하고,
**동시성 제어로 개선한 뒤 before/after를 비교**하기 위한 도구 모음.

> 한 줄 목적: "동시에 파파파팍 들어왔을 때 **언제 무너지고(포화점)**, **어디가 병목이며**,
> **동시성 제한으로 얼마나 건강해지는지**"를 숫자로 증명한다.

---

## 1. 어떻게 하는가 (How)

### 구성 파일
| 파일 | 역할 |
|------|------|
| `locustfile.py` | 가상 사용자 시나리오: 세션 생성 1회 → 추천 요청 반복. 입력은 `eval/dataset.jsonl` 메시지 샘플링 |
| `staged_shape.py` | 단계적 램프업 프로파일 (동시 5 → 10 → 25 → 50) |
| `capture_metrics.py` | 서버측 `/metrics` 스냅샷 + before/after 비교 (단계별 latency·폴백률 분해) |
| `run_load_test.sh` | 위를 묶은 오케스트레이션: before 스냅샷 → 부하 → after 스냅샷 → 비교 |
| `requirements.txt` | `locust` (앱 런타임과 분리) |

### 사전 준비
```bash
pip install -r load/requirements.txt          # locust 설치
# 앱이 떠 있어야 함 (Postgres/Redis/Neo4j/vLLM 연결 가능 상태)
# .env 호스트명이 docker용이면 로컬에선 localhost로 override
```

### 실행
```bash
# (권장) 한 방에: 스냅샷 → 단계적 부하(약 7분) → 스냅샷 → 비교
./load/run_load_test.sh                        # tag=baseline
TAG=after-semaphore ./load/run_load_test.sh    # 개선 후 재측정

# 수동 고정 부하 (동시 25명, 2분)
locust -f load/locustfile.py --headless --host http://localhost:8000 -u 25 -r 5 -t 2m

# 웹 UI (브라우저에서 사용자 수 슬라이더로 조절)
locust -f load/locustfile.py --host http://localhost:8000
```

### 측정 흐름 (오프라인 평가의 MLflow 루프를 인프라 측에 복제)
```
baseline 측정 → 포화점·병목 식별 → 동시성 제한 적용 → 재측정 → before/after 비교
```

---

## 2. 어떤 이점이 있는가 (Benefits)

1. **숨은 한계가 드러난다** — 단건 테스트로는 안 보이던 "동시 N명에서 p95가 SLA를 넘고
   타임아웃이 캐스케이드되는" 지점을 실제로 본다.
2. **병목을 데이터로 지목** — `recommend_stage_latency_seconds{stage}`로 extract / neo4j /
   llm_response 중 **무엇이 먼저 터지는지** 분해 → 추측이 아니라 근거로 개선.
3. **개선 효과를 정량 증명** — 동시성 제한(세마포어) 전/후 p95·처리량·에러율 표로 비교.
   "측정 → 개선 → 재측정"이라는 엔지니어링 루프를 인프라에서도 보여줄 수 있다.
4. **기존 관측 인프라와 즉시 연결** — Phase 2 메트릭 + Phase 3 로깅(trace_id) + Grafana/Loki가
   부하 중 실시간으로 살아 움직이는 걸 확인 (이미 만든 자산 활용).
5. **CDC 같은 '없는 문제'가 아니라 실재 병목을 푼다** — GPU 8B 단일 서빙 + 동시성 제어 부재는
   실제로 존재하는 문제. (데이터 변경이 거의 없어 CDC는 실익이 낮음)

---

## 3. 무엇을 신경 써서 봐야 하는가 (What to Watch)

### A. 클라이언트 측 (Locust `report.html` / CSV)
| 지표 | 왜 보나 / 위험 신호 |
|------|---------------------|
| **p95 / p99 latency** | 평균은 거짓말한다. **꼬리(tail)** 가 SLA(예: 10s)를 넘는 지점이 곧 포화점 |
| **처리량 (RPS)** | 사용자를 늘려도 RPS가 더 안 오르면 → **이미 포화** (GPU가 천장) |
| **실패율 (failures)** | 타임아웃/5xx 급증 지점 = 무너지는 동시성 수준 |
| **단계별 사용자 수와의 관계** | 5→10→25→50 중 **어느 단계에서 지표가 꺾이는지** |

### B. 서버 측 (`capture_metrics.py` diff / Grafana)
| 지표 | 왜 보나 |
|------|---------|
| **단계별 평균 latency** | extract / neo4j / **llm_response** 중 부하에서 폭증하는 단계 = 병목 (대개 LLM=GPU) |
| **폴백률(rule_based)** | 부하로 LLM 추출이 타임아웃 → 규칙기반으로 떨어진 비율. **↑ = LLM 과부하 신호** |
| **요청 결과 ok/error** | 서버가 집계한 성공/실패 (클라이언트 측 실패율과 교차검증) |

### C. 해석 포인트
- **포화점(saturation point)**: 사용자↑인데 RPS는 정체 + p95·에러율 급등하는 동시성 수준.
  → GPU 단일 8B 서빙이라 **비교적 일찍** 올 것. 그게 병목을 또렷이 보여주는 좋은 데모.
- **병목 귀속**: 거의 `llm_response`(생성) 단계일 가능성이 큼 → 다음 개선의 타겟.
- **개선 후 기대 모습**: 동시성 제한을 걸면 **에러율 급감 + p95/p99 안정**. 동작이
  "느리게 다 실패" → "안정적으로 처리 + 초과분은 빠르게 거절(429, 옵션)"로 건강해진다.
- **주의**: vLLM **콜드스타트**(GPU가 꺼져 있다 켜질 때 수십 초)를 포화로 오해 말 것.
  부하 전 워밍업 요청 1~2건으로 모델을 깨운 뒤 측정.

---

## 다음 단계 (개선 적용)
baseline 측정 후, LLM 호출 앞에 **동시성 제한(`asyncio.Semaphore`)** 을 넣고
(`config`에 `llm_max_concurrency` 추가) `TAG=after-semaphore`로 재측정해
`review/2026-06-XX-load-test.md`에 before/after를 기록한다.