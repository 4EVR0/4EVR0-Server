# P0 검증 마무리 준비 — 멀티턴 동등성·결정론적 가드

- 날짜: 2026-09-22
- 브랜치: `feat/p0-validation-closeout`
- 기준 리포트: `eval/results/v7-baseline-rubric2.json`
- 라이브 재실행: 원격 vLLM·Neo4j 주소 현재 해석 불가로 보류

## 1. 완료한 코드 변경

### Batch/SSE 대화 분기 공통화

기존 `recommend()`만 수행하던 대화 이력 로드, 후속 판정, 이력 만료 안내를
`_resolve_conversation_response()`로 분리해 `recommend_stream()`도 같이 사용한다.
후속 응답은 SSE에서 `meta → delta → done` 규약으로 전달된다.

### 멀티턴 검증기

- `eval/multiturn_dataset.jsonl`: 15개 시나리오
- `eval/run_multiturn_eval.py`: batch/SSE 동일 시나리오 실행
- 검사: 후속 제품 집합 유지, 이력 만료 안내, 전송 경로 제품 집합 동일성,
  요청 오류, 빈 응답, 한자 누출
- 리포트 메타데이터: code SHA, dataset SHA, model, prompt version

### 사람 라벨 비교 오류 수정

기존 안내는 라벨 수집 후 `run_response_eval.py --human-labels`로 응답을 새로 생성했다.
vLLM은 temperature 0에서도 응답이 달라질 수 있어, 사람이 채점한 응답 A의 라벨을
새 응답 B의 judge 점수와 비교하는 오류가 됐다.

`label_responses.py --calibrate REPORT LABELS`를 추가해 채점한 source report에 저장된
동일 응답·동일 judge 점수만 비교한다. 라벨의 `source_report`가 다르면 실패한다.

## 2. grounding 미달 4건 진단

| id | 진단 | 조치 |
|---|---|---|
| 3 | `FRAGRANCE_FREE` 요청이지만 제품 인증/전성분 근거가 없고, 후보에 `LINALOOL`도 포함 | 제약 근거 없이 제품 적합성을 추측하지 않고 제품 0건으로 처리 |
| 21 | id 3과 동일한 무향 조건 미검증 | 동일 |
| 14 | `MANDELIC ACID`는 전체 성분 근거에 있지만 추천 제품의 매칭 성분에는 없음. 주로 검색 적합성 문제 | 신규 기준선에서 검색 후보/각질 성분 포함 여부 재검증 |
| 36 | 전체 성분에만 있는 `BEESWAX`를 특정 제품에 연결해 설명 | 제품 bullet의 성분이 그 제품 `matched_ingredients`의 부분집합인지 코드 검증 |

## 3. 결정론적 출력 가드

1. **제품 0건**: LLM을 호출하지 않고 고정 안내문을 반환해 제품명 날조를 구조적으로 차단.
2. **미검증 제약**: 무향·알코올 프리·비건·저자극·EWG 제약이 있으면,
   현재 데이터로는 충족을 입증할 수 없으므로 제품을 노출하지 않고 확인 불가를 설명.
3. **한자**: batch·follow-up·SSE 청크에서 CJK 한자를 코드로 제거.
4. **제품-성분 연결**: `추천 제품` bullet의 제품명과 성분이 제공된 제품·
   `matched_ingredients`의 부분집합이 아니면 제공 데이터로만 조립한 고정 응답으로 교체.
5. **관측**: `recommend_output_guard_total{kind=...}`에 가드 발동을 유형별로 기록.

SSE 본문은 제품-성분 연결 검사 후 전송하기 위해 응답 생성을 완료한 뒤
하나의 `delta`로 보낸다. 성분/제품 `meta`는 생성 전에 먼저 전송되므로 카드 TTFT 개선은
유지되지만, 토큰 단위 본문 표시는 제품 근거 가드와 교환되었다. 신규 기준선에서
사용자 체감 지연을 다시 측정해야 한다.

## 4. 최신 SHA 품질 게이트

- 추출·생성·검색 리포트에 `code_sha`를 기록한다.
- `check_gate.py --expected-code-sha` 가 세 리포트의 SHA와 현재 CI SHA를 비교한다.
- `run-eval` 라벨이 유지된 PR에 새 커밋이 올라오면 `synchronize` 이벤트로 재실행한다.
- GitHub `main` 브랜치 보호 규칙 적용은 외부 설정 변경이므로 별도로 남아 있다.

## 5. 현재 검증

- 전체 유닛/계약 테스트: `113 passed`
- 정적 검사: `py_compile`, `git diff --check` 통과
- 남은 외부 검증:
  1. 사람 블라인드 채점 40건
  2. vLLM·Neo4j·Redis 연결 후 멀티턴 15개 batch/SSE 실행
  3. 변경된 가드 적용 후 신규 단일턴 50건 기준선
