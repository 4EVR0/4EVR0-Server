# 사람 블라인드 라벨링 도구 (P0-②)

- 날짜: 2026-09-10
- 브랜치: `fix/eval-session-isolation` (커밋 `07e2fdd`)
- 목적: "LLM judge 점수를 믿을 수 있냐"에 **숫자로** 답할 근거 확보
- 상태: 도구 완성. **실제 라벨 수집은 GPU로 v7 기준선을 다시 뽑은 뒤에 시작**

## 1. 왜 필요했나

`run_response_eval.py`에는 `--human-labels`, `calibrate_against_humans()`,
MAE·Pearson·Spearman이 **이미 구현돼 있었다.** 그런데 실제로 쓴 적이 없다.

```
$ find eval -iname "*human*"        → 없음
$ grep -l human_calibration eval/results/*.json → 0건 (리포트 34개 전부)
```

**총은 있는데 장전을 안 한 상태였다.** 이유는 단순하다 — 라벨을 만드는 수단이 없었다.

## 2. 만든 것 — `eval/label_responses.py`

### 블라인드

judge 점수·코멘트를 화면에 **절대 출력하지 않는다.** 모델 점수를 보고 채점하면
일치도가 부풀려져 검증이 무의미해진다(`LABELING.md`의 원칙).

테스트로 고정: `test_rendered_case_hides_judge_scores_and_comment`.

### judge와 같은 조건

judge-vs-human 비교가 성립하려면 **채점 입력이 같아야** 한다.

| 항목 | 어떻게 맞췄나 |
|---|---|
| 루브릭 | `response_judge.txt`의 `Dimensions:` 절을 그대로 추출해 표시 |
| 근거 컨텍스트 | `render_evidence_context()` 결과를 judge와 라벨러가 공유 |
| 채점 차원 | `DIMS` 5개 동일 |

### 편향 완화

- **표본**: judge 점수와 무관하게 무작위 추출. judge 점수로 고르면 일치도 추정이 편향된다
- **순서**: 제시 순서 셔플 — 앞뒤 응답 비교로 점수가 끌려가는 순서 효과 완화
- **2인 라벨링**: 같은 `--sample`·`--seed` 면 두 사람이 같은 표본을 본다

### 중단 복구

케이스마다 append + flush. 중간에 `q`로 나가도 직전까지 보존되고, 같은 명령을
다시 실행하면 남은 것만 묻는다.

### 라벨러 간 일치도

```bash
python eval/label_responses.py --agreement eval/labels/a.jsonl eval/labels/b.jsonl
```

차원별 MAE·Pearson·Spearman·완전일치율을 낸다. **이 값이 judge에 기대할 수 있는
현실적 상한선**이다 — 사람끼리도 안 맞는 차원이면 루브릭이 모호한 것이지 judge 탓이 아니다.

## 3. 부수적으로 고친 것 — 리포트에 근거 컨텍스트 저장

착수하자마자 막혔다. 리포트 케이스에 이것만 있었다.

```
['id', 'label', 'message', 'scores', 'overall', 'comment',
 'n_products', 'n_ingredients', 'response']
```

**제공된 제품·성분이 개수(`n_products: 5`)로만 남아 있었다.** 사람이 grounding
("추천 제품이 제공된 데이터 안에 있나")을 채점할 방법이 없다.

judge 컨텍스트 조립부를 `render_evidence_context()`로 분리하고, 그 결과를 케이스마다
`evidence` 필드로 저장하게 했다. judge에게 준 것과 **같은 문자열**임을 테스트로 고정
(`test_report_stores_evidence_context_for_human_labeling`).

기존 리포트에는 이 필드가 없다. 도구는 이때 "근거 컨텍스트 없음"을 표시해 grounding을
채점할 수 없음을 알린다(조용히 빈칸으로 두지 않는다).

## 4. 검증

신규 테스트 13건(라벨링 12 + 근거 저장 1). **전체 86개 통과.**

| 테스트 | 확인 |
|---|---|
| `test_same_seed_gives_two_labelers_the_same_sample` | 2인 라벨링 표본 일치 |
| `test_rendered_case_hides_judge_scores_and_comment` | 블라인드 |
| `test_rendered_case_flags_missing_evidence_context` | 옛 리포트에서 채점 불가 고지 |
| `test_rubric_comes_from_the_judge_prompt` | judge와 같은 루브릭 |
| `test_labels_are_readable_by_the_calibration_loader` | 출력이 `load_human_scores` 호환 |
| `test_quitting_saves_completed_cases_and_resumes` | 중단·이어하기 |
| `test_invalid_score_is_rejected_until_valid` | 0·6·문자 입력 거부 |
| `test_agreement_*` | 일치도 계산·표본 불일치 처리 |

실제 CLI로도 2인 라벨링 → 일치도 산출까지 왕복 확인했다.

## 5. 인프라 — 평가에 필요한 것

Tailscale 노드 상태(2026-09-10 확인)와 필요 여부.

| 노드 / 서비스 | 용도 | 평가에 필요? |
|---|---|---|
| `vast-gpu-server-2` | vLLM 서빙 | ✅ 필수 |
| `ip-172-31-56-102` | **Neo4j** (제품·성분 검색) | ✅ 필수 |
| `monitoring-server-1` | Prometheus/Grafana | ❌ 불필요 |
| Postgres | 세션 API | ❌ 불필요 (추천 경로 미사용, 확인함) |
| Redis | 응답 캐시 + 대화 이력 | ⚠️ 아래 참조 |

### Redis 주의

`.env`의 `REDIS_URL=redis://redis:6379`는 docker-compose 서비스명이라 **Mac에서 직접
실행하면 해석되지 않는다.** 캐시·대화 이력 모두 best-effort라 평가는 그대로 돌지만,

> **Redis 없이 세션 격리 A/B를 하면 결론이 틀린다.** 이력 저장 자체가 안 되므로
> `--session-mode shared` 도 오염 0으로 나와 "차이 없음"이라는 잘못된 결과가 된다.

A/B를 하려면:

```bash
docker run -d --name eval-redis -p 6379:6379 redis:7
# REDIS_URL=redis://localhost:6379
```

judge 키는 `.env.judge`(`JUDGE_MODEL`·`JUDGE_BASE_URL`·`JUDGE_API_KEY`)에 있다.
`.env`·`.env.judge` 모두 git 미추적이며 과거 이력에도 없음을 확인했다.

## 6. GPU 확보 후 순서

라벨링을 **지금 하면 안 된다.** 현재 최신 리포트는 운영이 쓰지 않는 base 프롬프트로
측정된 것이라(`review/2026-09-10-judge-grounding-diagnosis.md` 4장), 그 응답에 라벨을
달면 v7 재측정 시 라벨을 다시 만들어야 한다.

1. **v7 기준선 재측정** — `--judge-repeats 3` 함께 (`judge_repeat_stddev` 동시 확보)
2. **라벨링 40건** — 1번 리포트 대상. 여기서 `evidence` 필드가 채워져 나온다
3. (선택) 2인차 라벨링 → `--agreement`
4. **`--human-labels` 로 judge 검증** → MAE·Pearson·Spearman
5. 세션 격리 A/B (Redis 필요)

## 7. 면접 답변으로 쓸 수 있는 형태

> judge 점수를 그대로 쓰지 않고 judge부터 검증했습니다. 전문가 라벨 40건과 비교해
> Spearman·MAE를 냈고, 같은 답변을 3회 반복 채점해 표준편차를 구했습니다. 그 표준편차가
> 노이즈 하한이라, 그보다 작은 점수 차이는 회귀로 판정하지 않습니다.
>
> 라벨은 judge 점수를 가린 채, judge와 같은 루브릭·같은 근거 데이터로 받았습니다.
> 표본도 judge 점수와 무관하게 무작위로 뽑고 제시 순서를 섞었습니다. 두 사람이 같은
> 표본을 채점해 사람끼리의 일치도도 냈는데, 그게 judge에 기대할 수 있는 상한선입니다.

**주의:** 위 문장은 4번 단계까지 끝나야 쓸 수 있다. 지금은 도구만 준비된 상태이며,
숫자 없이 이렇게 말하면 안 된다.
