# 추천 품질 기준 — 어떻게 세웠고 어떻게 실험했는가

`llm_query_eval`의 A(프로덕션 고정 파이프라인) vs B(LLM이 즉석에서 만든 Cypher)
비교에서, **"뭘 좋은 추천이라고 볼 것인가"를 어떻게 정했는지**와 **실험을 실제로
어떻게 돌리는지**를 정리한 문서.

## 1. 왜 새로 기준을 세워야 했나

성분 단위(어떤 성분이 이 효능에 좋은가)는 `eval/gold_labels.py`가 이미 정답을
갖고 있었다 — 그래프의 `(Ingredient)-[:AFFECTS {evidence_type}]->(Effect)`
관계에서 `evidence_type == 'pubmed_evidence'`(논문 근거)인 것만 정답으로 침.

문제는 **사용자는 성분이 아니라 제품을 원한다는 것**. "여드름 흔적이랑 홍조가
고민이고 지성인데 지금 상황에 맞는 제품 추천해줘" 같은 실제 메시지에 성분
리스트만 돌려주는 건 실제 서비스가 하는 일이 아니다. 그런데 제품 단위 "정답"은
어디에도 없었다:

- `eval/gold_labels.py`는 성분 단위 정답만 있음
- `eval/product_category_eval.py`는 "카테고리가 적절한가"만 봤지, "이 제품이
  진짜 정답인가"를 재는 게 아니었음
- 손으로 "이 concern엔 이 제품이 정답"이라고 라벨링한 데이터는 존재하지 않음

그래서 제품 단위 채점 기준을 새로 만들어야 했다.

## 2. 기준을 세울 때 지킨 원칙

**같은 그래프로 정답도 만들고 채점도 하면 안 된다.** `eval/RESULTS.md` §3에
이미 지적된 문제인데 — 그래프의 `graph_score`/`evidence_type`을 정답 기준으로
쓰면, 애초에 그 그래프 데이터 자체가 잘못됐을 때는 못 잡아낸다. 그래서 가능하면
**그래프와 무관한 독립적인 신호**를 찾아서 같이 쓰기로 했다.

## 3. 찾아낸 독립 신호: 올리브영 실구매자 리뷰 태그

`Product` 노드에 `review_stats`라는 필드가 이미 있었다 — 올리브영 실제 구매자
리뷰에서 집계된 데이터다:

```json
{
  "피부타입": {"복합성에 좋아요": "50%", "건성에 좋아요": "40%", "지성에 좋아요": "11%"},
  "피부고민": {"진정에 좋아요": "42%", "보습에 좋아요": "57%", "주름/미백에 좋아요": "0%"},
  "자극도": {"자극없이 순해요": "76%", "보통이에요": "24%", "자극이 느껴져요": "0%"}
}
```

이건 우리 그래프의 AFFECTS/graph_score와 **전혀 무관하게**, 실제 구매자가 "이
제품 보습에 좋아요"라고 남긴 데이터다. 그래프가 틀렸어도 이 신호는 영향을 안
받는다는 점에서 독립적이다.

**한계**: `피부고민` 태그는 올리브영 리뷰 UI 자체의 고정 taxonomy라 3개
대분류(보습/주름·미백/진정)뿐이다. 우리 26개 concern 전체를 못 덮는다 —
ACNE, OILY_SKIN, PORE_CONGESTION처럼 이 3개 중 어디에도 안 들어가는 concern은
이 신호를 못 씀 (해당 시 `None` 처리).

## 4. 최종 채점 기준 3가지

`run_ab.py::score_products()`가 반환된 제품 목록마다 계산한다.

| 기준 | 계산 방법 | 데이터 출처 | 26개 concern 전체 커버? |
|---|---|---|---|
| **category_fit** | 반환된 제품 카테고리가 concern에 적합한 카테고리 목록(`_appropriate_categories`, 예: 여드름엔 크림 제외) 안에 있는 비율 | 프로덕션 코드(`recommend_service.py`)의 하드코딩 규칙 | O (그래프 아님, 하드코딩) |
| **ingredient_grounded** | 그 제품이 실제로 gold 성분(pubmed_evidence 근거)을 포함하는 비율 (`CONTAINS` 관계로 직접 확인) | 그래프(AFFECTS 엣지) | O (다만 그래프 의존) |
| **review_score** | 관련 concern이 리뷰 태그 3개 대분류 중 하나와 겹치면, 그 태그의 리뷰 비율 평균 | **올리브영 실구매자 리뷰** (그래프와 독립) | X (3개 대분류만) |

세 개를 동시에 보는 이유: `category_fit`+`ingredient_grounded`만 보면 "그래프
안에서는 그럴듯한데 실제로 사람들이 별로라고 하는 제품"을 못 걸러낸다.
`review_score`가 있으면 그런 케이스를 어느 정도 잡아낼 수 있다.

### review_score 태그 ↔ concern 매핑 (`run_ab.py::_REVIEW_CONCERN_MAP`)

```python
"보습에 좋아요":     DRY_SKIN, DEHYDRATED_SKIN, FLAKY_SKIN, BARRIER_DAMAGE
"주름/미백에 좋아요": WRINKLES, AGING_SIGNS, LOSS_OF_ELASTICITY, SAGGING_SKIN,
                    HYPERPIGMENTATION, UNEVEN_SKIN_TONE, DULLNESS, BLEMISHES,
                    DARK_CIRCLES, POST_ACNE_MARKS
"진정에 좋아요":     SENSITIVE_SKIN, REDNESS, IRRITATED_SKIN, ATOPIC_PRONE,
                    ROSACEA_PRONE, SUNBURN
```

"주름/미백"이 안티에이징과 미백을 한 태그로 묶어놓은 것처럼, 매핑이 완벽하게
깔끔하진 않다 — 올리브영 리뷰 UI의 taxonomy가 원래 그렇게 돼 있어서 그대로
따른 것.

## 5. 실험 설계 — A와 B를 각각 어떻게 만드는가

### A: 프로덕션이 실제로 하는 것 그대로

```
query_ingredients_by_effects(effects, min_graph_score)   # AFFECTS 1-hop
        ↓
apply_caution_filter(ingredients, concerns)               # 민감성 concern이면 CAUTION 성분 컷
        ↓
select_products(message, concerns, ingredient_scores)     # CONTAINS 1-hop + 카테고리 필터
                                                            # + 리뷰 재정렬 + 다양성 보장
```

전부 `app.clients.neo4j_client`/`app.services.recommend_service`의 **실제
함수를 직접 import해서 호출**한다 (텍스트를 복사하지 않음 — 이번 작업 중
`pg_experiment/queries.py`와 `eval/graphrag_ranking_eval.py`의 쿼리 복사본이
이미 프로덕션과 어긋나 있는 걸 발견했어서, 같은 실수를 반복하지 않으려고
내린 결정).

### B: LLM이 원문 메시지만 보고 직접 생성

`prompts/cypher_generation_products.txt`가 그래프 스키마(CONTAINS/AFFECTS/CAUTION
전부 포함) + effect_code 15개 목록 + 카테고리 값 + few-shot 3개를 준다. LLM은:

1. 메시지에서 관련 effect_code를 스스로 판단
2. `Product-CONTAINS->Ingredient-AFFECTS->Effect` 경로로 제품까지 직접 조회하는
   Cypher를 생성 (성분에서 멈추지 말라고 명시)
3. 부적합 카테고리(클렌징 등)를 스스로 거를지 판단
4. **메시지에 "민감성", "저자극", "자극 없이" 같은 회피 신호가 있으면 CAUTION
   관계까지 확인해서 그 성분이 든 제품을 제외할지, 정해진 hop 수 없이 스스로
   판단** — 이게 이 실험이 원래 보려던 것("자유도를 주면 LLM이 질문에 맞게
   탐색 깊이를 조절하는가")에 제일 가까운 부분
5. `{"cypher": ..., "params": ...}` JSON으로 응답

`generate.py`가 실행 전 `EXPLAIN`으로 문법만 검증하고, 실패하면 에러 메시지를
붙여서 1회만 재생성한다.

**hop 카운팅 주의점**: `run_ab.py::count_hops()`는 Cypher 문자열에서 `-[`
개수를 세는 정규식 근사치다. 기본 2-hop 쿼리(CONTAINS+AFFECTS)는 정확히 2로
나오지만, CAUTION을 `NOT EXISTS {...}` 서브쿼리로 추가하면 그 안에서 CONTAINS를
한 번 더 타서 **4**로 나온다(관계 종류는 3개뿐인데 카운트는 4) — "관계가 몇
종류 쓰였는가"가 아니라 "쿼리 문자열에 관계 패턴이 몇 번 나오는가"를 잰다는
점을 감안해서 해석해야 한다.

**CAUTION 경로가 실제로 테스트되는 시나리오 비율**: 46개 중 12개(26%)가
민감성 계열 concern(`SENSITIVE_SKIN`, `REDNESS`, `IRRITATED_SKIN`,
`ATOPIC_PRONE`, `ROSACEA_PRONE`, `BARRIER_DAMAGE`)을 포함해서, A의
`apply_caution_filter`가 실제로 발동한다. 새로 회피 조건 질문을 손으로 만들
필요 없이, `dataset.jsonl`에 이미 이 비율만큼 자연스럽게 섞여 있다 —
프로덕션의 CAUTION 적용 자체가 사용자가 "피해주세요"라고 명시했는지가 아니라
concern 종류로 자동 판단되는 방식이라(`_is_sensitivity_query`), 이 12개
시나리오가 실제 분포를 그대로 반영한다.

### 질문(테스트 시나리오)

`4EVR0-Server/eval/dataset.jsonl`을 재사용한다 — 원래 프로필 추출 평가용으로
이미 있던 정답셋인데, "피지는 많은데 속은 건조하고, 좁쌀과 막힌 모공에 여드름
자국까지 있어요"처럼 **여러 고민이 한 문장에 섞인 실제 발화 스타일**이라 이
실험에도 그대로 맞는다. concern이 비어 있는 시나리오(순수 네거티브 케이스)는
빼고 46개를 쓴다.

## 6. 실행 방법

```bash
cd 4EVR0-Server/llm_query_eval
python run_ab.py --limit 3   # 시나리오 3개만 먼저 (파이프라인 확인)
python run_ab.py             # 46개 전체
```

시나리오마다 이렇게 출력된다:

```
[id  1 DRY_SKIN+WRINKLES+LOSS_OF_ELAS] A: cat_fit=1.00 grounded=1.00 review=0.54  |  B: cat_fit=0.80 grounded=0.60 review=0.41 hops=2  (8.2s)
```

전체 끝나면 A/B 평균 요약 + `results/ab_<timestamp>.json`(케이스별 상세, 생성된
Cypher 원문 포함)이 남는다.

## 7. 지금까지 실행하며 배운 것 (시행착오)

실제로 도는 걸 보면서 고친 것들 — 앞으로 비슷한 실험할 때 참고용:

1. **few-shot 예시와 테스트 질문이 겹치면 안 됨**: 처음에 few-shot 질문을
   실제 테스트 질문에서 그대로 가져다 써서, 그 케이스들은 LLM이 "생성"한 게
   아니라 "베껴 쓴" 것이었음. 겹치지 않는 예시로 교체.
2. **Qwen3 "생각하기" 모드를 꺼야 함**: 프로덕션 코드는 전부
   `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`를 쓰는데
   이 실험 스크립트만 빠져 있었음 → 생각하는 데 토큰을 다 써서 JSON 응답이
   잘리는 실패로 이어졌던 것으로 보임. 추가함.
3. **진단 정보(B가 고른 params, 실패 시 raw 응답)를 처음부터 로그에 남겨야
   함**: 안 남겨서 "B가 왜 A보다 점수가 낮은지" 원인을 못 찾은 적이 있었음.
4. **질문 자체가 너무 단순하면(concern 1개짜리) hop이 항상 1로 고정됨**:
   당연히 그래야 정답인 구조라서, "자유도를 주면 hop이 늘어나는가"라는 원래
   질문 자체가 테스트가 안 됐음. `dataset.jsonl`의 다중 고민 시나리오로
   바꾸고, B의 목표 자체를 "제품까지 가라"로 바꾸고 나서야 진짜 2-hop 이상이
   나올 여지가 생김.
5. **성분 단위 정답만으로는 제품 추천 품질을 못 잼**: 이 문서 §1~4가 그
   해결 과정.
6. **`CONCERN_EFFECT_MAP` 사본도 원본과 어긋나 있었음**: `run_ab.py`가
   `eval/gold_labels.py`의 `PRODUCTION_CONCERN_EFFECT_MAP`(원본을 손으로 베낀
   사본 — `eval/`이 다른 저장소라 예전엔 import가 안 됐음)을 썼는데, 실제
   원본(`app/services/taxonomy_normalization_service.py::CONCERN_EFFECT_MAP`)과
   대조하니 4개 concern(OILY_SKIN/HYPERPIGMENTATION/DULLNESS/WRINKLES)이 이미
   달라져 있었음. `llm_query_eval`이 `4EVR0-Server` 안에 있어서 원본을 바로
   import할 수 있으므로 사본 대신 원본을 씀 — A 쿼리·LLM 클라이언트에 이어
   **같은 "복사본 드리프트" 패턴이 이걸로 벌써 5번째**로 발견됨.
7. **"hop"은 그래프에서 실제로 타는 관계 수**: Product-Ingredient-Effect-Concern을
   전부 잇는 전체 개념 모델은 4노드/3관계(hop)지만, 지금 쿼리는 Concern을
   그래프로 안 탄다 — concern→effect 변환이 `CONCERN_EFFECT_MAP`(파이썬 딕셔너리
   조회)로 이뤄지고, 그 결과(effect_code 목록)를 `$effects` 파라미터로 Cypher에
   꽂아 넣을 뿐이라 RELATES_TO 관계를 안 씀. 그래서 기본 제품 쿼리는
   CONTAINS+AFFECTS 2개 관계 = 2-hop. RELATES_TO를 안 쓰는 이유는 그래프에
   26개 concern 중 8개가 이 관계 자체가 없어서(`eval/RESULTS.md` §1) — 프로덕션이
   원래부터 우회한 것.

## 8. 아직 다루지 않은 것

- **`run_bd.py`(B vs D, RDB 버전과 비교)**: A vs B 결과가 B 우위로 뚜렷하게
  나올 때만 착수 예정.
- **속도 벤치마크 재실행**: `pg_experiment`가 이미 프로덕션 쿼리와 어긋나 있는
  상태 — 별도 과제.
- **`review_score`의 26개 concern 완전 커버**: 올리브영 리뷰 UI 자체의
  taxonomy 한계라 이 실험에서 확장하긴 어려움. 대안이 필요하면 별도 검토.
