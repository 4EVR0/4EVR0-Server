# P0 검증 마무리

이 문서는 `v7-baseline-rubric2.json`의 judge 타당성과 멀티턴 서비스 경로를
검증하는 재현 절차다.

## 현재 판정 (2026-09-22)

- 사람 블라인드 채점: **40/40 완료**
- 기존 judge 교정: 핵심 3축(`concern_fit`, `grounding`, `korean_quality`) 케이스 평균 기준
  MAE `0.753`, Pearson `0.4835`, Spearman `0.4615`; judge 평균 `4.886`,
  사람 평균 `4.133`으로 `+0.753` 과대평가
- 라이브 멀티턴/신규 단일턴: 원격 vLLM·Neo4j 중단으로 대기

핵심 3축은 약한~중간 수준의 방향성이 있지만 절대점수 편향과 5점 쏠림이 커서,
기존 judge 점수는 릴리스 승인 게이트가 아닌 추세 관측용으로만 사용한다.
간결성·형식 준수는 사람 기준 자체가 모호했다는 라벨러 회고에 따라 보조 진단값으로 본다.
상세 결과와 수동 검수 피드백은 `review/2026-09-22-p0-validation-closeout.md`에 기록한다.

## 1. 사람 블라인드 채점 40건 (완료)

- 대상 리포트: `eval/results/v7-baseline-rubric2.json`
- SHA-256: `4142b2a288b2b140b2cec6ace953660bf2b6e7a1cdbc4fca8fccc7063a191d2a`
- 표본 선정: `sample=40`, `seed=23`
- 케이스 순서: `27, 11, 31, 40, 8, 4, 37, 49, 35, 5, 39, 42, 10, 26, 46, 45, 48, 22, 43, 32, 33, 12, 44, 24, 21, 17, 36, 14, 16, 41, 3, 7, 47, 30, 15, 1, 29, 18, 13, 9`

채점 명령:

```bash
PYTHONPATH=. python eval/label_responses.py \
  --report eval/results/v7-baseline-rubric2.json \
  --labeler hyeokjun-v7-rubric2 \
  --sample 40 \
  --seed 23 \
  --out eval/labels/hyeokjun-v7-rubric2.jsonl
```

각 응답에 대해 `concern_fit`, `grounding`, `conciseness`, `korean_quality`,
`format_adherence`를 1~5점으로 채점한다. judge 점수와 코멘트는 화면에 보이지
않는다. `q`로 종료해도 저장되며, 같은 명령으로 다시 실행하면 이어서 채점한다.

채점 후에는 응답을 새로 생성하지 말고, 반드시 라벨을 만든 같은 리포트와 비교한다.

```bash
PYTHONPATH=. python eval/label_responses.py \
  --calibrate eval/results/v7-baseline-rubric2.json eval/labels/hyeokjun-v7-rubric2.jsonl \
  --calibration-out eval/results/v7-rubric2-human-calibration.json
```

가능하면 두 번째 라벨러가 같은 `sample`/같은 `seed`로 채점한 뒤 사람 간 일치도를
judge-vs-human 일치도의 상한으로 함께 보고한다.

## 2. 멀티턴 batch/SSE 기능 검증

데이터셋은 15개 시나리오와 `new`, `followup`, `missing_history` 분기를 포함한다.

```bash
GEN_TEMPERATURE=0 RECOMMEND_CACHE_ENABLED=false PYTHONPATH=. \
python eval/run_multiturn_eval.py \
  --transport both \
  --out eval/results/multiturn-v7.json
```

자동 검사:

- 후속 질문의 제품 집합이 직전 추천 집합과 동일한지
- 이력이 없는 후속 질문이 명시적 안내로 종료되는지
- batch와 SSE의 케이스별 제품 집합이 동일한지
- 중앙 `hard_checks.py`의 빈 응답·한자·미검증 제약·제품/성분 근거·제품 목적·
  브랜드 중복·금칙 표현 검사

단일 응답 리포트도 같은 검사기를 사용하며 케이스별 `hard_failures`와 집계
`hard_failure_rate`를 기록한다. CI 통과 기준은 `hard_failure_rate == 0`이다.

## 3. P0 종료 조건

- 사람 채점 40건과 judge-vs-human MAE/Pearson/Spearman 보고
- 멀티턴 15개 시나리오의 batch/SSE 기능 차이 0건
- 후속 질문의 이전 제품 이탈 0건
- 검색 공백 제품 날조 0건과 한자 누출 0건
- 결정론적 Hard gate 실패율 0
- 리포트에 code SHA, dataset SHA, 모델, 생성 프롬프트 버전 기록
