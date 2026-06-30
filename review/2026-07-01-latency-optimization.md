# Latency 최적화 — 측정 결과 (2026-07-01)

> 계획: `review/2026-07-01-latency-optimization-plan.md`. 이 문서는 **각 phase의 측정 결과**를 누적한다.
> 단건 저부하 분해(부하 테스트 아님). 도구: `load/latency_bench.py`, span 메트릭 `recommend_latency_span_seconds`.

## P0. Baseline — 단건 latency 분해 (캐시 miss = 실제 GPU 경로)

조건: 캐시 ON·세마포어 N=8·프리픽스 캐싱 ON(서버), 단건 순차 10회(매번 신규 문장 → miss), 워밍업 제외.

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

**상태:** P0 완료. 다음 = P1(스트리밍 + TTFT 측정).
