# feat(llm_query_eval): 제품 단위 A/B 비교 전환 + 그래프 RELATES_TO 보강

커밋 범위: `9bd3ed1` → `fc8434a`

## Summary

기존엔 A/B가 성분 단위로만 비교됐는데, 실제 사용자는 성분이 아니라 제품을 원하므로 제품 단위 비교로 전환했다. 그 과정에서 A/B가 공정하게 비교되도록 그래프 데이터 공백도 같이 메웠다.

### 1. 제품 단위 비교 전환 (9bd3ed1)

A는 프로덕션 실제 파이프라인을 그대로 재현한다. `query_ingredients_by_effects` → `apply_caution_filter` → `select_products` 순서로, 전부 `app.services.recommend_service`/`app.clients.neo4j_client` 함수를 직접 호출한다(텍스트 복사 없음). B는 `prompts/cypher_generation_products.txt`를 새로 만들어서, LLM이 성분에서 멈추지 않고 `Product-CONTAINS→Ingredient-AFFECTS→Effect`까지 직접 조회하는 Cypher를 생성하도록 바꿨다.

채점 기준도 3가지를 새로 만들었다. `category_fit`은 반환된 제품 카테고리가 고민에 적합한지를 `recommend_service`의 `_appropriate_categories`를 재사용해서 판단하고, `ingredient_grounded`는 그 제품이 실제로 gold 성분(pubmed_evidence)을 포함하는지를 본다. `review_score`는 올리브영 실구매자 리뷰 태그(`Product.review_stats`, 보습/주름·미백/진정)와 겹치는 비율인데, 그래프 자체 데이터와 무관한 독립 검증 신호라 그래프가 틀려도 걸러낼 수 있다(해당 concern이 없으면 None으로 처리).

### 2. 정답 기준 드리프트 수정 + CAUTION 3-hop 예시 (69f0c46)

`run_ab.py`가 쓰던 `eval/gold_labels.py`의 `PRODUCTION_CONCERN_EFFECT_MAP`은 원본을 손으로 베낀 사본이었는데, 실제 원본(`taxonomy_normalization_service.py::CONCERN_EFFECT_MAP`)과 대조해보니 4개 concern에서 이미 어긋나 있었다. 그래서 사본 대신 원본을 직접 import하도록 고쳤다. 그리고 `prompts/cypher_generation_products.txt`에 CAUTION 관계를 노출하고, 민감성이나 저자극 관련 언급이 있으면 CAUTION으로 성분을 배제하는 3번째 관계를 쓰는 예시도 추가했다 — 기존엔 CAUTION을 아예 안 보여줘서 LLM이 쓸 기회 자체가 없었다.

### 3. 그래프 RELATES_TO 보강 + Concern 경로 자유도 (2f0f275)

그래프의 `Concern` 노드가 15개뿐이었고(앱 enum은 26개) `RELATES_TO`로 Effect와 연결된 것도 7개뿐이라, 26개 concern 중 19개는 애초에 그래프로 도달할 방법이 없는 상태였다. 그래서 `sync_relates_to.py`를 새로 만들어서 `CONCERN_EFFECT_MAP`을 그래프의 `RELATES_TO` 관계로 직접 반영했다(MERGE 기반이라 다시 실행해도 안전). 47건을 추가하고 기존과 어긋나 있던 3건은 삭제했으며, 프로덕션 서빙 경로는 `RELATES_TO`를 읽지 않으므로 실서비스에는 영향이 없다.

이제 B 프롬프트가 Concern 26개 목록도 같이 제공하고, "Concern을 매치하지 말라"던 기존 지침은 제거했다 — Concern 경로(RELATES_TO)로 갈지 Effect를 바로 고를지는 메시지에 맞게 LLM이 직접 판단한다. `generate.py`도 EXPLAIN(문법만 확인)만으로 검증하던 걸 실제 실행으로 바꿔서, 결과가 0건이면 실패로 보고 재시도하도록 고쳤다 — 조건이 너무 빡빡해서 아무것도 안 나오는 쿼리를 "성공"으로 잘못 판정하던 문제였다.

### 4. 문서 (fc8434a)

`QUALITY_CRITERIA.md`에는 채점 기준을 왜 이렇게 정했는지, hop 상한이 이론상 최대 5라는 것 등 상세 설계 내용을 담았고, `PRESENTATION.md`는 발표용으로 옮겨 담기 쉽게 정리한 요약본이다.

## Test plan

- [x] 로컬 Neo4j로 A(제품 파이프라인) 정상 동작 확인
- [x] `sync_relates_to.py` 적용 후 26개 concern 전부 RELATES_TO 확인
- [ ] GPU 서버에서 `python run_ab.py --limit 3`으로 B 실제 생성 확인 (로컬은 GPU 서버 미연결이라 B는 "생성 실패" 경로만 검증됨)
- [ ] 46개 시나리오 전체 실행 → A vs B 지표 비교
