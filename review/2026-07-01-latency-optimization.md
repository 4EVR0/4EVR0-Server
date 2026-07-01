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

**상태:** P2 완료(앱 변경 없음, 결과만 기록).

---

## ⚠️ GPU 재프로비저닝 & 재baseline (2026-07-01 오후)

**경위:** GPU 반납 후 재대여 → 매번 **다른 Vast.ai 인스턴스**가 배정됨.
- 1차 재대여: `nvidia-smi`에 `20.7GB/24GB 사용 중 · 프로세스 없음` = **공유/부분 GPU**(다른 테넌트가 VRAM 점유) → 가용 ~3.8GB로 9B 가중치(~18GB)조차 못 올려 **vLLM EngineCore OOM 크래시.**
- 2차 재대여: **전용 RTX 3090 24GB**(`4MiB/24576MiB`, 텅 빔) 확보 → 정상 기동.

**측정 카드 (기록 — 재현성):** `NVIDIA GeForce RTX 3090` 24GB, driver 595.58.03, vLLM 0.23.0, `Qwen/Qwen3.5-9B`.
> 교훈: **절대 latency는 카드마다 다르다.** 앞으로 카드 스펙을 문서에 남겨 "같은 스펙이냐"를 즉시 대조.

### 재baseline — RTX 3090 (P0/P1 갱신)

| 지표 | 이전 카드(오전) | **RTX 3090(현재)** | Δ |
|------|---:|---:|---:|
| P0 total (p50) | 10.08s | **12.19s** | +21% |
| P0 generate | 8,719ms | **10,707ms** | +23% |
| P0 extract | 1,131ms | 1,583ms | +40% |
| P1 TTFT (p50) | 1.86s | **2.72s** | +46% |
| P1 generate_ttft(prefill) | 617ms | 1,066ms | — |
| P1 generate_decode | 8,040ms | 8,889ms | +11% |

→ **어제 실험 카드보다 ~20~25% 느림 = 동일 스펙 아님.** 그래서 재baseline이 맞았다. **이후 P3~는 이 RTX 3090 기준.**

### 구조적 결론은 카드 불변 (중요)
절대 ms는 올랐지만 **비중·정성 결론은 그대로**:
- **generate/decode 지배:** generate 86.4%(이전 87.7%), decode 76%.
- **스트리밍 체감:** TTFT 2.72s vs total 11.81s → **~4.3배**(이전 5.4배).
- **생성 prefill 작음:** 1,066ms = 9%(이전 6%) → 프롬프트/KV 레버 천장 여전히 한 자릿수 %.
- **retrieval 무의미:** 96ms = 0.8%.

→ **P0·P1·P2의 결론(decode가 병목, 스트리밍이 체감 레버, 프롬프트/KV는 음성)은 카드가 바뀌어도 유지됨.** 바뀐 건 절대값뿐.

**상태:** 재baseline 완료(RTX 3090).

---

## P3. 출력 토큰↓ (decode 직접 공략) + 품질 게이트

**가설:** decode(P1의 병목)는 출력 토큰 수에 비례 → 생성 프롬프트를 더 간결하게(v5) 만들면
출력이 줄어 decode↓. 단 출력이 바뀌므로 **품질 회귀를 judge로 게이트**해야 채택.

**v5 프롬프트:** v4(제품 3개·~900자) → **제품 2개·~400자**로 축소(grounding 규칙은 유지). `GEN_PROMPT_NAME`로 선택.

### latency (스트리밍 벤치, RTX 3090, 10건)
| 지표 | v4 | v5 | Δ |
|------|---:|---:|---:|
| 평균 출력 토큰 | ~238 | **96** | −60% |
| generate_decode | 9,088ms | **3,671ms** | **−60%** |
| total (p50) | 11.99s | **6.37s** | **−47%** |
| TTFT | 2.67s | 2.78s | ~동일(prefill·extract 불변) |

→ **출력 토큰 = decode임을 정확히 확인** (출력 −60% → decode −60%, 선형).

### 품질 (judge=gpt-4o-mini, 20건, temp=0)
| 차원 | v4 | v5 | Δ |
|------|---:|---:|---:|
| concern_fit | 4.75 | 4.15 | −0.60 |
| **grounding** | 4.55 | 3.80 | **−0.75** |
| conciseness | 3.85 | 3.95 | +0.10 |
| korean_quality | 4.80 | 4.45 | −0.35 |
| **format_adherence** | 4.65 | 3.85 | **−0.80** |
| **OVERALL** | **4.52** | **4.04** | **−0.48** |

### 판정 — v5 채택 불가 (품질 게이트 실패)
- **−45~47% latency**를 얻지만 **OVERALL 4.52→4.04(−0.48)**, 특히 **grounding −0.75·format −0.80**.
  너무 terse해서 근거 인용·형식이 무너짐 → 프로덕션 채택 불가.
- **확인된 원리:** 출력 토큰↓는 decode를 선형으로 줄이는 **진짜 latency 레버가 맞다.** 그러나 **품질 바닥**이
  존재해 무한정 줄일 수 없다 — 속도·품질 트레이드오프.
- **중요한 맥락:** **P1 스트리밍이 이미 체감 latency(TTFT 2.7s)를 해결**했으므로, 스트리밍 응답에선
  total 축소의 UX 이득이 작다 → 품질을 깎아가며 total을 줄일 유인이 약하다.

### P3-b. v6 스윗스팟 — 채택 ✅
v5 실패 교훈 반영: 축소는 **"사용 팁" 삭제 + ~600자 + 성분 설명 압축**으로만, **grounding 규칙·제품 3개·format은 verbatim 유지.**

**v4 / v5 / v6 종합:**
| | v4(기존) | v5(과함) | **v6(채택)** |
|------|---:|---:|---:|
| 출력 토큰 | ~238 | 96 | **139** |
| generate_decode | 9,088ms | 3,671ms | **5,623ms** |
| **total (p50)** | 11.99s | 6.37s | **8.35s (−30%)** |
| TTFT | 2.67s | 2.78s | 2.70s |
| concern_fit | 4.75 | 4.15 | 4.70 |
| **grounding** | 4.55 | 3.80 | **4.55 (v4와 동일)** |
| conciseness | 3.85 | 3.95 | 3.85 |
| korean_quality | 4.80 | 4.45 | 4.75 |
| format_adherence | 4.65 | 3.85 | 4.45 |
| **OVERALL** | **4.52** | 4.04 | **4.46** |

**판정 — v6 채택 (기본값 전환):**
- **latency total −30%(12.0→8.35s)** 확보하면서 **OVERALL 4.52→4.46(−0.06, v4 CI 4.18~4.79 안 = 유의차 없음)**.
- **grounding 4.55로 v4와 완전 동일** — verbatim grounding 규칙이 핵심(v5는 이걸 안 지켜 −0.75).
- format만 −0.20(허용). → **품질 유지 + 속도 확보 = 스윗스팟.**
- `config.gen_prompt_name` 기본을 `recommend_response.v6`으로 전환.

### P3 결론
- **출력 토큰↓는 decode를 선형으로 줄이는 진짜 latency 레버** — 단 **품질 바닥이 존재**(v5가 증명).
- **스윗스팟(v6)**: grounding·format 구조는 지키고 분량만 줄이면 **−30% latency를 품질 손실 없이** 얻는다.
- 방법론적 핵심: **judge 게이트가 "얼마나 줄여야 하는지"의 경계를 데이터로 그어줌**(v5 반려 → v6 채택).

**상태:** P3 완료(v6 채택, 기본값 전환).

---

## P4. extract 경량화 — guided/구조화 decoding (LLM 유지) — 음성

**방향(포트폴리오 취지):** "규칙기반 우선"은 LLM을 안 쓰는 방향이라 LLMOps 취지와 어긋남 → 철회.
**LLM을 유지한 채** extract(TTFT 최대 항목, ~1.6s)를 줄이는 시도로 **guided decoding**(출력을
프로필 JSON 스키마·유효 enum에 강제)을 실험. `EXTRACT_GUIDED_DECODING` 토글, app·`run_eval` 공용
(`build_extract_extra_body`).

**A/B (추출 eval, 50건, guided OFF vs ON):**
| 지표 | OFF | ON | Δ |
|------|---:|---:|---|
| concern F1 | 0.8743 | 0.8743 | **0** |
| skin_type 정확도 | 0.98 | 0.98 | 0 |
| constraint F1 | 0.963 | 0.963 | 0 |
| **무효값(오타) 비율** | **0.0** | **0.0** | 0 |
| **폴백/에러 비율** | **0.0** | **0.0** | 0 |
| extract latency p50 | 1.105s | 1.138s | ~0 |
| 출력 토큰 | 28.6 | 28.6 | 0 |

### 결과 — guided decoding 채택 안 함 (no-op)
- **latency 이득 0** (예측대로 — guided는 decode 속도 레버가 아님).
- **신뢰성 이득도 0** — guided가 푸는 "무효 JSON/잘못된 enum" 문제가 **이 파이프라인엔 애초에 없다**:
  `json_object` 모드 + 튜닝된 프롬프트로 이미 **무효값 0%·폴백 0%**. temp=0이라 출력이 스키마를
  이미 100% 만족 → guided가 제약할 게 없음(F1 바이트 단위로 동일).
- **LLMOps 교훈:** 구조화 decoding은 *무효 출력 문제가 있을 때* 값지다. **측정으로 그 문제가 없음을
  확인 → 불필요한 복잡도를 안 더한다.** "기법을 언제 *안* 쓸지 아는 것"도 데이터로 판단.
- extract latency(~1.6s)는 **9B로 태스크를 돌리는 본질 비용** → 진짜 레버는 서빙쪽(작은 모델 라우팅·FP8),
  이는 범위 밖(§11). 코드(스키마·토글)는 실험 아티팩트로 유지, 기본 OFF.

**상태:** P4 완료(음성 — guided 미채택). 다음 = P5(종합 + PR).
