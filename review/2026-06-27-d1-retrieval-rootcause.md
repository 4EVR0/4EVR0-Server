# 2026-06-27 작업 리뷰 (D1: 검색 품질 — 근본 원인 규명)

## 한 줄 결론
응답 grounding 폭락(C0의 1.90)의 근본 원인은 **앱이 아니라 지식 그래프의 성분→효능 데이터 결함**.
`eval → 검색 → 효능매핑 → 그래프 구축 파이프라인` 까지 추적 완료. 앱 쿼리/프롬프트로는 해결 불가.

> 측정 인프라(C0 eval)가 띄운 신호를 따라 내려가 **데이터 파이프라인의 결함**까지 도달한 사례.

---

## 1. 추적 경로 (위 → 아래로 좁힘)
| 단계 | 확인 | 상태 |
|------|------|------|
| 응답 품질(C0) | grounding 1.90/5 | 🔴 문제 |
| 응답 프롬프트(A2) | 강화해도 1.90→2.26 | 프롬프트로 미해결 |
| 추출 | `ENLARGED_PORES, OILY_SKIN` | ✅ |
| 효능 매핑 | `SEBUM_REGULATION, KERATOLYTIC, ANTI_INFLAMMATORY` | ✅ |
| **Neo4j 효능→성분** | 피지 효능에 안티에이징 성분 반환 | ❌ **근본 원인** |

## 2. 진단 (Neo4j 직접 쿼리)

**테스트 케이스**: "모공이 넓고 피지가 많아요" → 효능 `SEBUM_REGULATION` 등

### (a) RETINOL은 SEBUM에 연결조차 안 됨
- `RETINOL` 이 AFFECTS 하는 효능: ANTI_INFLAMMATORY / SOOTHING / HYDRATING / ANTI_AGING …
- → 모공/피지 케이스의 효능 중 **광범위한 `ANTI_INFLAMMATORY`를 타고 끼어든 것.**

### (b) SEBUM_REGULATION 데이터 자체가 빈약·오염
- 연결 성분: **pubmed 근거 2개**(1위가 ACETYL HEXAPEPTIDE-8 = 주름 펩타이드, 오라벨), **cosing 155개는 전부 graph_score=0**.

### (c) 진짜 피지 성분이 그래프에 잘못 들어감
| 성분(실제 피지/모공 핵심) | 그래프 연결 효능 | 문제 |
|------|------|------|
| NIACINAMIDE | DEPIGMENTING, BARRIER_REPAIR | SEBUM_REGULATION 연결 **없음** |
| SALICYLIC ACID | KERATOLYTIC, BARRIER (score 0) | sebum 라벨 없음, 점수 0 |
| ZINC PCA | BARRIER, HYDRATING | sebum 아님 |

## 3. 근본 원인 (2가지, 둘 다 데이터 계층)
1. **성분→효능(`AFFECTS`) 엣지가 부정확** — 핵심 성분이 맞는 효능에 연결 안 되고, 엉뚱한 성분이 연결됨.
2. **`graph_score`가 대부분 0** — pubmed 근거 성분만 점수가 있고, cosing_function 다수는 0 → 랭킹이 사실상 "pubmed 있는 소수"로만 결정 → 그 소수가 틀리면 결과 전체가 틀림.

→ 둘 다 **그래프를 만드는 파이프라인(GraphRAG/INCI)** 의 산출물 품질 문제. 앱(WAS)의 책임 범위 밖.

## 4. 해결 옵션
| | 무엇 | 한계/효과 |
|--|------|----------|
| **앱-side 완화(band-aid)** | ① 여러 효능 동시 만족 성분 우대 ② score=0 cosing 노이즈 제외 ③ 주효능 가중 | 데이터 천장에 막힘 — 좋은 성분이 애초에 없으면 못 꺼냄 |
| **상류 데이터 수정(근본)** | 성분↔효능 엣지 재구축 + graph_score 산정 개선 (cosing도 점수, 오라벨 교정) | 진짜 해결. 단 GraphRAG/INCI 파이프라인 작업(큼) |

## 5. 시사점 (포트폴리오)
- **추천 품질의 천장은 지식 그래프 품질이 결정.** 앱 레벨 최적화(프롬프트/쿼리)로는 데이터 결함을 못 넘는다.
- 측정(C0 eval) → 근본원인(데이터 파이프라인)까지의 **end-to-end 추적**이 핵심 가치. "왜 추천이 이상한가"를 추측이 아니라 데이터로 규명.

## 다음 단계 (결정 필요)
- (A) 앱-side 완화 실험 — 쿼리 랭킹 개선(multi-effect 우대 + score>0 필터). C0 eval로 grounding before/after 측정. 빠르지만 천장 존재.
- (B) 상류 데이터 수정 — `AFFECTS` 엣지/score 재구축 (GraphRAG/INCI). 근본적이나 범위 큼.
- 권장: **(A)로 천장을 먼저 확인**(쿼리만으로 어디까지 되나) → 부족분을 (B)로. 둘 다 C0 eval로 검증.
