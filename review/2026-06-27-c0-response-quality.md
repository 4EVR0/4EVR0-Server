# 2026-06-27 작업 리뷰 (C0: 응답 품질 평가기 + baseline)

## 오늘 한 일
추출 평가(run_eval)가 못 보던 **생성된 추천문 자체의 품질**을 재는 평가기를 만들고(LLM-as-judge),
현재 응답 프롬프트의 baseline을 측정. **"grounding 폭락"이라는 숨은 문제를 적발.**

> 이 시스템의 "응답"(사용자가 실제로 보는 추천문)은 그동안 **측정된 적이 없었다.** C0가 그 공백을 메움.

---

## 1. 구축 (C0)

| 산출물 | 내용 |
|--------|------|
| `app/prompts/response_judge.txt` | 심판 루브릭 (5개 차원, 1~5점, JSON 반환) |
| `eval/run_response_eval.py` | 파이프라인 호출 → judge 채점 → 집계 → MLflow(`4evr0-response-quality`) |

- **HTTP/세션 없이 `recommend_service.recommend()` 직접 호출** → Postgres/Redis 불필요, Neo4j+vLLM만으로 동작.
- 평가 차원: `concern_fit`, `grounding`, `conciseness`, `korean_quality`, `format_adherence` + overall.
- 추적: gen_prompt_version(평가 대상 응답 프롬프트) + judge_prompt_version 기록 → 향후 A2 비교 대비.

## 2. Baseline 결과 (gen_prompt `76af3f07`, judge `4603be1a`, 19케이스)

| 차원 | 점수 |
|------|------|
| concern_fit | 4.74 / 5 |
| **grounding** | **1.90 / 5** 🔴 |
| conciseness | 4.11 / 5 |
| korean_quality | 4.32 / 5 |
| format_adherence | 4.53 / 5 |
| **OVERALL** | **3.92 / 5** |
| 응답생성 p50 | 11.86s |

## 3. 핵심 발견 — grounding 폭락 (검증됨)

나머지 4개 차원은 모두 4점대인데 **grounding만 1.90** (대부분 1점). 실제 응답+judge 코멘트로 검증:

- **id1 (grounding=1)**: judge — "추천 제품·성분이 제공 데이터와 전혀 일치하지 않음(허위정보)".
  → 모델이 제공된 5개 제품 대신 **제품명을 지어냄**(환각).
- **id6 (grounding=5)**: 응답이 "제공된 제품엔 모공 축소 성분이 없다"고 정직히 인정 + 외부 성분 권유.
  → 정직하나, **제공 제품이 고민에 애초에 안 맞음**.

### 두 겹의 원인
1. **생성 환각** — 응답이 제공 제품을 안 쓰고 임의 제품을 제시(id1). → **A2(응답 프롬프트)** 로 교정 가능.
2. **검색 미스매치** — Neo4j가 concern에 안 맞는 제품을 반환(id6). → **D1(검색 품질)**, 더 깊은 문제.

즉 "grounding 1.9"는 **생성 + 검색** 두 문제로 분해된다. (추출 eval/GPU 메트릭으론 안 보이던 문제)

## 4. 한계 (정직하게)
- **self-eval 편향**: 심판이 생성과 같은 모델(Qwen3.5-9B) → 자기선호 가능. 결과는 **상대 비교용**.
  더 엄밀히는 다른/상위 모델을 심판으로.
- 응답 생성 temperature=0.3 → run마다 약간 변동. (production 동작 그대로 평가)
- 19케이스 단일 run. 단, grounding 신호가 매우 크고(대부분 1점) 코멘트로 교차검증돼 **방향성은 확실**.

## 다음 단계 (C0가 unlock한 실험)
- **A2 (응답 프롬프트 v2)**: "제공된 제품만, 정확한 이름으로. 적합 제품 없으면 솔직히" 강조
  → 같은 정답셋 재측정 → grounding ↑ 확인 (MLflow `4evr0-response-quality`에서 비교)
- **D1 (검색 품질)**: concern→effect→ingredient→product 매칭 점검 (제품이 고민에 맞게 오는지)
- 심판을 외부/상위 모델로 → self-eval 편향 제거
