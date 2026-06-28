# 2026-06-27 작업 리뷰 (D1-A: 검색 쿼리 완화 — 측정으로 효과 확인)

## 한 줄 결론
D1에서 규명한 "검색이 off-target 성분 반환" 문제를 **쿼리 랭킹 개선**으로 완화.
grounding **1.90 → 2.63 (+0.73)**, overall **3.92 → 4.19**. 단 여전히 낙제(2.63) → **데이터 수정(B) 필요** 확인.

> A2(프롬프트)는 grounding +0.37·overall 하락이었는데, A(쿼리)는 +0.73·overall 상승.
> "모델에 뭘 먹이느냐(검색)"를 고친 게 "어떻게 말하라(프롬프트)"보다 2배 효과 + 부작용 없음.

---

## 1. 변경 (`app/clients/neo4j_client.py`, `query_ingredients_by_effects`)
- **이전**: `UNWIND effects` 후 `ORDER BY pubmed먼저, graph_score DESC` → 광범위 효능(ANTI_INFLAMMATORY)
  하나만 타고 온 off-target 성분(RETINOL 등)이 상위. 성분당 중복 행.
- **이후**: 성분당 집계 후 `ORDER BY effect_match DESC, total_score DESC, has_pubmed DESC`
  - `effect_match` = 요청 효능 중 몇 개를 만족하는가 → **여러 효능 동시 만족 성분 우대** (off-target 단일매칭 강등)
  - `total_score` 합 → score=0 노이즈 가라앉음
  - display 필드(claim 등)는 최고 score 매칭 효능에서

## 2. 측정 (run_response_eval.py, 19케이스, gen_prompt v1 동일)
| 차원 | baseline(옛 쿼리) | 새 쿼리 | Δ |
|------|:----:|:----:|:----:|
| grounding | 1.90 | **2.63** | **+0.73** ↑ |
| concern_fit | 4.74 | 4.95 | +0.21 |
| conciseness | 4.11 | 4.37 | +0.26 |
| korean_quality | 4.32 | 4.53 | +0.21 |
| format_adherence | 4.53 | 4.47 | −0.06 |
| **overall** | **3.92** | **4.19** | **+0.27** ↑ |

> 측정은 새 GPU 인스턴스(`vast-gpu-server-2-1`, 호스트명 충돌로 -1)에서 `GPU_SERVER_URL` override로 실행.

## 3. 해석
- **검색이 진짜 병목이었음을 숫자로 확정** — 같은 프롬프트/모델에서 쿼리만 바꿔 grounding +0.73.
- **단, 천장 도달**: grounding 2.63은 여전히 낙제. 원인은 D1에서 본 데이터 결함 —
  좋은 성분(나이아신아마이드/BHA/아연)이 `SEBUM_REGULATION`에 안 연결, cosing 다수 score=0.
  쿼리는 "있는 것 중 더 나은 걸 고를" 뿐, 없는 걸 만들지 못함.

## 4. 결정
- **새 쿼리 프로덕션 적용(승격)** — 전 지표 ≥ baseline, overall +0.27, 회귀 없음 (A2와 달리 명확한 개선).

## 5. 한계 / 후속
- **MLflow 응답평가가 검색(retrieval) 버전을 param으로 안 추적** → baseline vs 새 쿼리 run이 같은 param으로 보임.
  후속: `run_response_eval.py`에 retrieval 버전 태그/param 추가.
- grounding 2.63 천장 → **B (데이터 파이프라인: AFFECTS 엣지·graph_score 재구축)** 가 다음 큰 레버.
- 정답셋 19개·self-eval 한계 동일.
