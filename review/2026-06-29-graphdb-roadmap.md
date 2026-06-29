# 2026-06-29 — GraphDB(Neo4j) 디벨롭 로드맵 & 기여 정리

추천 엔진의 지식그래프(Neo4j)를 담당하면서 **이미 한 일**과, **어디를 더 디벨롭하면
임팩트가 큰지**를 정리한다. (담당자 본인의 기여 서사 + 다음 작업 우선순위)

---

## 0. 이미 기여한 것 (기여 서사)

`review/neo4j-schema.md` 가 증거. "Neo4j 설치"가 아니라 추천 품질의 데이터 기반을 책임졌다.

1. **도메인을 그래프로 모델링** — 추천 로직 자체인 다음 경로를 스키마로 설계:
   `(Product)-[:CONTAINS]→(Ingredient)-[:AFFECTS]→(Effect)-[:RELATES_TO]→(Concern)`
2. **근거 기반(explainable) 추천 설계** — `AFFECTS` 관계에 `graph_score`/`evidence_type`
   (pubmed_evidence > cosing_function)/`paper_count` 를 실어 "논문 N편 근거" 추천을 가능케 함.
3. **코드 ↔ 실제 스키마 불일치 10건 수정** — `HAS_EFFECT`→`AFFECTS`, Claim/Paper 노드 부재→
   관계 속성으로 대체, `eligibility_tier`→`evidence_type` 등 (`neo4j-schema.md §5`).
4. **Cypher 쿼리 작성·검증** — 근거 우선순위 정렬(pubmed 우선, `graph_score DESC`) + 결과 검증.

> 한 문장: **"고민→효능→성분→제품 지식그래프를 설계·구축하고, 논문 근거를 관계에 실은
> 근거 기반 추천을 모델링했으며, 코드-실제 스키마 불일치 10건을 잡아 정합성을 맞췄다."**

---

## 1. 더 디벨롭하는 방향 (구체적으로)

진짜 도약 지점은 평가에서 이미 나왔다 — **A2 실험의 결론이 "진짜 병목은 검색 품질(D1)"**
(`review/2026-06-27-a2-grounding.md`). 그게 정확히 GraphDB 담당 영역이다.

| 디벨롭 방향 | 무엇 | 왜 (근거) |
|------------|------|-----------|
| **D1: 검색 품질 개선** 🎯 | "아이크림이 모공에 추천되는" 미스매치 수정. concern→effect 매핑·랭킹 보정 | A2 review가 지목한 **실제 병목**. 가장 임팩트 큼 |
| **검색 품질 평가기** | 추천된 제품이 고민에 맞는지 재는 eval (지금 없음) | 추출/응답 평가는 있는데 **검색 평가가 공백** |
| **concern 커버리지** | DB 15종 vs enum 26종 불일치(11종 누락) 메우기 | `neo4j-schema.md §4`에 적힌 미해결 갭 |
| **랭킹 고도화** | graph_score 가중치 튜닝, 다중 effect 교집합 스코어링 | "왜 이 순서로 추천?"을 정교하게 |
| **쿼리 성능** | 인덱스, 프로파일링 (이미 `duration_ms` 로깅 있음) | 부하 테스트와도 연결됨 |

### 누락된 concern 11종 (커버리지 갭, `neo4j-schema.md §4`)
`PORE_CONGESTION`, `ENLARGED_PORES`, `FLAKY_SKIN`, `ROUGH_TEXTURE`, `UNEVEN_SKIN_TONE`,
`BLEMISHES`, `DARK_CIRCLES`, `SUNBURN`, `WRINKLES`, `LOSS_OF_ELASTICITY`, `SAGGING_SKIN`

---

## 2. 추천 우선순위

**1) D1(검색 품질) + 2) 검색 품질 평가기** 부터.

이유:
- 평가가 "여기가 진짜 병목"이라고 **이미 객관적으로 지목**했다 (A2 negative result).
- GraphDB 담당의 **고유 영역**이다 (프롬프트/서빙이 아니라 데이터·매칭 문제).
- 평가기까지 만들면 **"측정 → 개선 → 재측정"** 루프를 본인 손으로 완성 →
  기존 추출/응답 평가의 공백(검색 평가)을 메워 **평가 체계 완결**.

### D1을 어떻게 시작하나 (제안)
1. **현상 수집** — A2 judge 코멘트에서 미스매치 케이스 추출(id2 피지 성분 없음, id7 아이전용→모공 추천 등).
2. **원인 분해** — concern→effect 매핑이 틀렸나? effect→ingredient `graph_score`가 부적절한가?
   product 매칭(`matched_count`)이 카테고리를 무시하나?
3. **검색 품질 평가기** — 케이스별 "추천 제품이 고민에 적합한가"를 점수화(룰 기반 or LLM-judge).
4. **개선 → 재측정** — 매핑/랭킹/카테고리 필터 보정 후 같은 평가로 before/after 비교.

---

## 관련 문서
- `review/neo4j-schema.md` — 실제 스키마/관계/코드 불일치 기록
- `review/2026-06-27-a2-grounding.md` — D1을 다음 병목으로 지목한 negative result
- `review/2026-06-27-c0-response-quality.md` — grounding 폭락을 생성+검색 두 문제로 분해