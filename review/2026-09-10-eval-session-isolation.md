# 응답 평가 세션 격리 (P0-①)

- 날짜: 2026-09-10
- 브랜치: `fix/eval-session-isolation` (커밋 `6275c4c`)
- 배경: `docs/LLMOps_취업대비_발전방안.md` 2장 A항 — "독립 평가 사례가 같은 대화 세션을 공유"
- 범위: `eval/run_response_eval.py`, `eval/README.md`, `tests/test_eval_reliability.py`
- 미수행: GPU·Neo4j 실측. 아래 오염 경로는 정적 코드 분석으로 확인한 것이며,
  실제 점수 영향은 A/B 실행으로 측정해야 확정된다.

## 1. 문제

`run_response_eval.py:257`이 모든 케이스에 같은 세션 ID를 넘기고 있었다.

```python
for case in cases:
    rec = await recommend("eval-response", case["message"], gen_prompt)
    #                      ^^^^^^^^^^^^^^ 50개 케이스가 공유
```

`recommend()`(`recommend_service.py:577-603`)의 실행 순서가 문제를 만든다.

| 순서 | 동작 | 위치 |
|---|---|---|
| 1 | `load_recent(session_id)` — 대화 이력 로드 | `:577` |
| 2 | 이력 있음 + 후속 판정 → `_handle_followup()` | `:578-579` |
| 3 | 이력 없음 + 후속 표현 → "이전 추천 못 찾음" 안내문 | `:582-588` |
| 4 | 응답 캐시 조회 | `:592` |
| 5 | 정상 파이프라인(추출 → Neo4j → 생성) | `:606~` |

2번으로 빠지면 **Neo4j 검색을 통째로 건너뛴다.** `_handle_followup`(`:529-566`)은
`last["products"]` — 즉 **직전 케이스가 추천한 제품** — 을 그대로 재사용해 답변을 만든다.

```python
last = next((t for t in reversed(history) if t.get("products")), None)
products = _reorder_by_ranking(_reconstruct_products((last or {}).get("products", [])), ...)
```

### 오염이 성립하는 조건

- `conversation_enabled = True` (`config.py:28`, 기본값)
- Redis 정상
- `_is_followup()`이 참으로 판정

### 응답 캐시 OFF로는 막히지 않는다

대화 이력은 `conv:v1:{session_id}` 라는 **별도 Redis 키**(`conversation_store.py:24`)에
저장되고 TTL은 기본 7200초(`config.py:30`)다. 캐시를 꺼도 이력은 남고,
**2시간 안에 같은 평가를 다시 돌리면 이전 실행의 이력까지 물려받는다.**

### 영향 범위

`recommend()`를 호출하는 평가 스크립트는 `run_response_eval.py` 하나뿐임을 확인했다
(`grep -rn "eval-response"` → 1건). 추출 평가(`run_eval.py`)·검색 평가
(`run_retrieval_eval.py`)는 `recommend()`를 거치지 않아 해당 없음.

## 2. 조치

### 케이스별 세션 격리

```python
session_id = f"eval-{run_id}-{case['id']}"      # isolated 모드
await conversation_store.clear(session_id)       # 실행 전 정리
...
finally:
    await conversation_store.clear(session_id)   # 실행 후 정리(성공·실패 무관)
```

`run_id = {타임스탬프}-{uuid 6자리}` 를 실행마다 새로 부여해, 이력 TTL 안에
재실행해도 실행 간 이력이 섞이지 않게 했다.

### 오염을 사후에 증명할 근거 기록

케이스마다 `recommend()` 호출 **직전**의 이력 길이를 남긴다.

| 필드 | 위치 | 의미 |
|---|---|---|
| `session_id` | cases[] | 그 케이스가 쓴 세션 |
| `history_len_before` | cases[] | 실행 시점에 남아 있던 턴 수 |
| `contaminated_cases` | metrics | `history_len_before > 0` 인 케이스 수 |
| `contamination_rate` | metrics | 위 비율 |
| `run_id` / `session_mode` | run | 실행 식별자와 격리 모드 |
| `conversation_enabled` / `conversation_ttl_seconds` | run | 오염 가능 조건 |

격리 실행이면 `contaminated_cases == 0` 이어야 한다. 리포트만 보고 그 실행이
격리된 조건이었는지 판별할 수 있게 하는 것이 목적이다.

### `--session-mode shared`

격리 이전 동작(`eval-response` 공유, 이력 미정리)을 그대로 재현한다.
**오염 영향 측정 전용**이며 기준선 생성에 쓰지 않는다. 요약 출력과 리포트에
경고가 함께 찍힌다.

## 3. 검증

`tests/test_eval_reliability.py`에 5개 추가 (전체 71개 통과, 회귀 없음).

| 테스트 | 확인 내용 |
|---|---|
| `test_isolated_mode_gives_each_case_a_clean_session` | 세션 3개 모두 상이, `history_len_before` 전부 0, 실행 후 이력 잔여 없음, 세션당 clear 2회 |
| `test_isolated_sessions_differ_between_runs` | 두 실행의 `run_id`·세션 집합이 겹치지 않음 |
| `test_shared_mode_reproduces_cross_case_contamination` | 이력 길이가 `[0, 1, 2]` 로 누적, `contaminated_cases == 2` |
| `test_run_records_session_isolation_conditions` | run 정보에 격리 조건 기록 |
| `test_run_rejects_unknown_session_mode` | 잘못된 모드 거부 |

`shared` 테스트의 `[0, 1, 2]` 누적이 **기존 동작에 오염이 있었다는 직접 증거**다.

```
$ python -m pytest tests/ -q
71 passed, 2 warnings in 1.41s
```

## 4. 다음 단계에서 확인할 것

### A/B 실측 (GPU·Neo4j 필요)

```bash
python eval/run_response_eval.py --session-mode shared   --out eval/results/shared.json
python eval/run_response_eval.py --session-mode isolated --out eval/results/isolated.json
```

확인할 것:

1. `shared`의 `contaminated_cases` 실제 값
2. 두 실행의 `resp_grounding` 차이
3. 케이스 순서를 바꿔도 `isolated` 점수가 유지되는지 (격리의 최종 확인)

### grounding 2.263 가설

`resp-20260627-035306.json` 기준 `resp_grounding = 2.263`으로 5개 차원 중 유독 낮다
(다음으로 낮은 `conciseness`가 3.895). 후속 오판으로 빠진 케이스는 검색 없이
직전 케이스의 제품으로 답하므로 근거성이 낮게 나오는 것이 자연스럽다.

**다만 이건 아직 가설이다.** A/B에서 `isolated`의 grounding이 유의하게 오르지 않으면
원인은 다른 데 있다 — judge 프롬프트의 근거성 기준이 과하게 엄격한 경우가 유력한 대안이다.
(3단계 작업에서 판별)

## 5. 면접 답변으로 쓸 수 있는 형태

> 평가 코드에서 모든 케이스가 한 대화 세션을 공유하고 있었습니다. 추천 서비스가
> 이력을 먼저 읽고 후속 질문으로 판정되면 검색을 건너뛰기 때문에, 뒤쪽 케이스가
> 앞 케이스의 제품으로 답하는 경로가 있었습니다. 응답 캐시를 꺼도 이력은 별도
> Redis 키라 막히지 않았고, TTL 2시간 안에 재실행하면 이전 실행 이력까지
> 유입될 수 있었습니다.
>
> 케이스별 세션 격리로 고치고, 리포트에 `contaminated_cases` 를 남겨서 그 실행이
> 격리된 조건이었는지 사후에 확인할 수 있게 했습니다. 격리 이전 동작을 재현하는
> 모드를 남겨서 오염이 점수에 준 영향도 측정할 수 있게 했습니다.

## 6. 미커밋 작업 보존

작업 시작 시점에 `app/services/recommend_service.py`·`app/static/index.html`에
미커밋 변경(지시적 후속 질문 처리, `review/2026-07-25-deictic-followup-context.md`)이
있었다. 이번 작업과 파일이 겹치지 않아 **그대로 두고** `eval/`·`tests/` 만 커밋했다.
