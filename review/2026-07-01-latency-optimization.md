# Latency 최적화 — 측정 결과 (2026-07-01)

> 계획: `review/2026-07-01-latency-optimization-plan.md`. 이 문서는 **각 phase의 측정 결과**를 누적한다.
> 단건 저부하 분해(부하 테스트 아님). 도구: `load/latency_bench.py`, span 메트릭 `recommend_latency_span_seconds`.

## P0. Baseline — 단건 latency 분해 (캐시 miss = 실제 GPU 경로)

**측정 조건**
- 앱: 캐시 ON · 세마포어 N=8 · 프리픽스 캐싱 ON(서버). 모델 `Qwen/Qwen3.5-9B`.
- 부하 아님 — **단건 순차 10회**, 매 요청 신규 문장(`base + uuid`)으로 **캐시 miss 강제** → 실제 GPU 경로.
- 워밍업 1건 분포 제외. 도구: `python load/latency_bench.py --n 10 --mode miss`.
- 계측: `recommend_latency_span_seconds{span}` 증분 평균 + `trace_id` 구조 로그.

**원시 분포 (클라이언트 total, 초):** 10.04 / 9.18 / 10.09 / 11.48 / 10.08 / 8.88 / 10.23 / 11.55 / 9.36 / 8.95
→ mean 9.98 · p50 10.08 · p90 11.55 · max 11.55.

**span 로그 샘플(trace_id 1건):**
```
latency_trace cache=miss cache_lookup=0.5ms extract=1130.6ms retrieval=77.2ms
              generate=8110.3ms gate_wait=0.0ms total=9320.0ms overhead=1.3ms
```

**span별 평균 (서버 측, 10건 증분):**

| span | 평균(ms) | 비중 | 메모 |
|------|---:|---:|------|
| cache_lookup | 0.6 | 0.0% | Redis GET |
| **extract** (LLM #1) | **1,131** | **11.4%** | 짧은 JSON 추출 |
| retrieval (neo4j) | 89 | 0.9% | **비병목 — 측정으로 확인** |
| gate_wait | 0.0 | 0.0% | 단건이라 큐 대기 없음(부하 시만 의미) |
| **generate** (LLM #2) | **8,719** | **87.7%** | **지배적** — 아직 prefill/decode 미분리 |
| overhead | 2.3 | 0.0% | normalize+prompt_build+postprocess 합 — **~0 확인** |
| **total** | **9,942** | 100% | p50 10.08s, p90 11.55s |

### 즉시 도출되는 사실 (baseline만으로)
1. **생성이 88%.** 단건 latency는 사실상 `generate`가 전부다. → latency 레버는 생성을 건드려야 의미.
2. **retrieval(neo4j) 0.9% — 검색 최적화는 무의미**(가정이 아니라 우리 숫자로 확인). [음성 #6 ✔]
3. **overhead 2.3ms — normalize/prompt_build/postprocess는 ~0**. 개별 span으로 쪼갤 가치 없음을 실측으로 증명 → `overhead` 한 칸으로 묶은 결정이 옳았음.
4. **extract 11.4%(1.1s)** — 무시 못 할 둘째. LLM #1 경량화(레버 #5)의 여지.
5. **gate_wait 0** — 단건엔 큐 없음. 부하 구간에서 따로 봐야 할 값(이전 부하 테스트에서 extract에 섞여 보였던 것).

### 아직 모르는 것 → 다음 단계
- `generate` 8.7s 중 **prefill vs decode 비중**은 미분리. 가설(1절): prefill은 소수, decode가 지배.
- → **P1 스트리밍**으로 TTFT를 얻어 `generate = prefill + decode`로 쪼개면, "프롬프트 길이↓·KV 캐싱이
  왜 효과 작은지(prefill이 작아서)"를 **우리 숫자로** 증명할 수 있다.

**상태:** P0 완료.

---

## P1. 스트리밍(SSE) + TTFT — generate를 prefill/decode로 분리

**구현:** `POST /api/v1/recommend/stream`(SSE). `meta`(구조 데이터 즉시) → `delta`(생성 토큰) → `done`.
생성 단계를 `generate_ttft`(첫 토큰까지=프리필+1토큰) / `generate_decode`(나머지)로 분리 계측.
조건은 P0와 동일(단건 10회, miss). 도구: `python load/latency_bench.py --n 10 --mode stream`.

**클라이언트 측 (체감):**
| 지표 | 값 |
|------|---|
| **TTFT** | p50 **1.86s** · p90 2.26s · mean 1.91s |
| total | p50 10.05s · p90 12.74s (P0와 동일 — 스트리밍은 total 불변) |

→ **체감 latency 10s → 1.9s (~5.4배).** 사용자는 빈 화면 대신 성분·제품을 즉시 보고 답변이 흘러나온다.

**서버 측 span (10건 평균, ms):**
| span | 평균(ms) | 비중 |
|------|---:|---:|
| cache_lookup | 0.5 | 0% |
| extract | 1,174 | 11.8% |
| retrieval | 83 | 0.8% |
| **generate_ttft** (생성 prefill+1토큰) | **617** | **6.2%** |
| **generate_decode** | **8,040** | **81.1%** |
| overhead | 2.2 | 0% |
| total | 9,916 | 100% |

### 핵심 — 1절 가설 증명 (이게 P1의 진짜 산출물)
- **decode가 81%, 생성 prefill은 6.2%.** 즉 **프롬프트 길이↓·KV/프리픽스 캐싱은 generate_ttft(전체의
  ~6%)만 건드리므로 단건 latency 천장이 ~6%**다 — 가정이 아니라 **우리 측정값**으로 확정.
- **진짜 레버는 decode(81%):** 출력 토큰↓(P3) · 토큰당 속도 FP8(범위 밖). 그리고 **체감은 스트리밍(P1)**.
- TTFT(1.9s) 구성: extract 1.17s + retrieval 0.08s + generate_prefill 0.62s. → TTFT를 더 줄이려면
  **extract 경량화(P4)** 가 최대 항목.

**상태:** P1 완료. 다음 = P2(프롬프트 길이↓·KV 단건 — 음성 실증, 천장 ~6% 확인).
