# 2026-06-27 작업 리뷰 (A2: 응답 grounding 개선 시도 — negative result)

## 오늘 한 일
C0가 적발한 **grounding 폭락(1.90/5)**을 응답 프롬프트로 고쳐보려 했으나,
**프롬프트만으로는 안 됨**을 측정으로 확인. 진짜 원인을 judge 증거로 규명 → 다음 타겟(D1) 도출.

> "싼 수정(프롬프트)을 먼저 시도 → 측정으로 천장 확인 → 데이터로 진짜 병목 규명" 의 한 사이클.

---

## 1. 가설 & 변경
- 가설: 응답이 제공 제품을 안 쓰고 환각함 → "제공된 제품만, 정확한 이름으로, 없으면 솔직히"를 강제하면 grounding ↑.
- `app/prompts/recommend_response.v2.txt` (`06ec368b`): STRICT grounding 규칙 추가.
- `recommend_service.recommend(..., gen_prompt_name=None)` + `run_response_eval.py --gen-prompt`
  으로 응답 프롬프트 교체 가능하게(프로덕션 기본 동작 유지).

## 2. 결과 (MLflow `4evr0-response-quality`, judge `4603be1a`, 19케이스)

| 차원 | v1(76af3f07) | v2(06ec368b) | Δ |
|------|:----:|:----:|:----:|
| grounding | 1.90 | 2.26 | +0.37 ↑ (여전히 낙제) |
| concern_fit | 4.74 | 4.47 | −0.26 ↓ |
| conciseness | 4.11 | 3.90 | −0.21 ↓ |
| format_adherence | 4.53 | 4.05 | −0.47 ↓ |
| korean_quality | 4.32 | 4.37 | +0.05 |
| **overall** | **3.92** | **3.81** | **−0.10 ↓** |

→ **grounding은 거의 안 움직였고(여전히 2.26/5) overall은 오히려 하락.** 프롬프트 개선 실패.
→ **v2 프로덕션 승격 안 함.** (production은 v1 유지)

## 3. 원인 규명 (judge 코멘트 정성 분석)

두 가지 실패 모드가 공존:

**① 생성 환각 — 모델이 "제공 제품만" 제약을 안 지킴** (다수 케이스)
- id5/10/15: "추천 제품들이 제공 데이터에 존재하지 않음 — 심각한 할루시네이션"
- id16: 없는 '로즈마리 추출물', id19: 없는 'ASCORBIC ACID' 언급
- → 9B 모델이 강한 지시에도 grounding 제약을 **신뢰성 있게 따르지 못함**.

**② 검색 미스매치 — Neo4j가 고민에 안 맞는 제품 반환**
- id2: "제공 목록에 피지 조절 성분이 전혀 없음"
- id7: "제공 제품이 '아이' 전용인데 모공/여드름용으로 추천됨"
- → 근거 댈 **좋은 제품이 애초에 검색되지 않음**.

## 4. 결론 & 다음 타겟
- **프롬프트(A2)는 grounding의 천장** — 두 원인 모두 프롬프트로 해결 불가.
- 더 큰 레버리지:
  - **D1 (검색 품질)** — concern→effect→ingredient→product 매칭 점검. "아이크림이 모공에 추천되는" 구조적 문제.
  - (생성측) 더 강한 제약(structured output로 product_id 선택만 허용) 또는 상위 모델(A3).
- self-eval 편향 한계는 여전 — 절대 점수보다 **v1 vs v2 상대 비교**로 해석.

## 산출물
- `app/prompts/recommend_response.v2.txt` (실험 아티팩트, 승격 안 함)
- `recommend_service` gen_prompt 파라미터화, `run_response_eval.py --gen-prompt`
- MLflow `4evr0-response-quality`: v1/v2 run 2건
