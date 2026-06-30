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

**상태:** P1 완료.

---

## P2. 프롬프트 길이↓ · KV/프리픽스 캐싱 — 단건 latency 음성 실증

**방법:** prefill 거동은 vLLM 속성이라 **앱 변경 없이 vLLM에 직접** 호출해 TTFT(=prefill+1토큰)를
잰다. 시스템 프롬프트 길이를 바꿔가며(고유 nonce 접두로 prefix 교차오염 방지) median-of-3.
`cold` = 새 프리픽스(캐시 미스), `warm` = 동일 프롬프트 재호출(프리픽스 전체 hit).

| 시스템 프롬프트(char) | 토큰~ | cold TTFT | warm TTFT (프리픽스 hit) |
|---:|---:|---:|---:|
| 50 | 25 | 0.462s | 0.450s |
| 750 | 375 | 0.500s | 0.500s |
| 1,500 | 750 | 0.570s | 0.460s |
| 3,000 | 1,500 | 0.890s | 0.680s |

> 비교 기준: 단건 total ≈ **9.9s**, decode ≈ **8.0s** (P0/P1).

### 결과 — 가설 그대로 (음성 실증)
- **프롬프트 길이 ↓ (레버 #2):** 25→1,500 토큰(60배)으로 늘려도 TTFT 0.46→0.89s, **증가분 ~0.43s뿐.**
  우리 실제 프롬프트(~750토큰) prefill은 ~0.5s → **통째로 없애도 total의 ~5%, 현실적 축소는 ~1~2%.**
- **KV/프리픽스 캐싱 (레버 #3):** cold vs warm 차이가 **0.1~0.2s**(1,500–3,000 토큰 구간). 단건 total의 ~1~2%.
- **공통 이유:** 둘 다 **prefill만** 건드리는데, prefill은 ~0.5~0.9s로 **decode(8s) 대비 작다**(P1: 6.2%).
  → 단건 latency를 못 움직인다. **가정이 아니라 직접 측정으로 확정.** [음성 #2·#3 ✔]

### 부하 결과와의 정합 (중요)
프리픽스 캐싱은 **부하에선 +12% throughput**(§load-test 10절)인데 **단건 latency엔 ~0**이다 — 모순 아님.
- 부하: 수십 요청이 같은 프롬프트 prefill **연산을 동시 경합** → 재사용이 GPU 연산을 풀어줌(throughput↑).
- 단건: 경합 없음, prefill 절대량(0.5s)이 작아 체감 없음.
- **결론: 프리픽스 캐싱은 "throughput 레버지 단건 latency 레버가 아니다."** 같은 기능이 *지표/상황에 따라* 다르게 보인다.

**상태:** P2 완료(앱 변경 없음, 결과만 기록). 다음 = P3(출력 토큰↓ — decode 81% 직접 공략, 품질 eval 게이트).
