# 2026-09-23 독립 평가셋

- 파일: `2026-09-23.jsonl` (30건, ID 101~130)
- SHA-256: `64aff8da51f626891196fc98b8953ebd209eed84f1dcf8d44bc9229d610c8c7c`
- 목적: 반복 튜닝에 사용한 `eval/dataset.jsonl` 50건과 분리해 현재 서비스의 일반화 품질을 한 번 검증한다.
- 구성: 피부 고민 단일/복합 요청, 5개 제품 조건, 고민 없음·부정 표현, 한영 혼용.

문항과 정답 프로필은 첫 실행 전에 고정했다. 평가 결과를 보고 이 파일의 라벨이나 문장을
수정하지 않는다. 불일치가 발견되면 원인을 기록하고 다음 버전의 독립 평가셋을 만든다.
이 평가셋의 응답을 제품 정책·프롬프트 튜닝에 사용한 시점부터는 새로운 독립 검증셋이 필요하다.

평가 절차:

1. `load_dataset`으로 형식·enum을 검증하고 SHA-256을 확인한다.
2. 현재 코드 SHA에서 추출·검색·응답을 각각 실행한다. 응답은 세션을 격리하고 캐시를 끈다.
3. 자동 검사 결과와 케이스별 실패를 기록한다. 기존 개발셋의 임계값을 이 평가셋에 맞춰 변경하지 않는다.
4. 사람이 저장된 **같은 응답**을 `concern_fit`, `grounding`, `korean_quality` 세 축으로 블라인드 채점한다.
5. 사람 채점 후 동일 응답의 Judge 점수와 MAE·편향·상관을 비교한다. Judge 절대 점수는 릴리스 게이트로 사용하지 않는다.

사람 채점 명령 예시:

```bash
python eval/label_responses.py \
  --report /private/tmp/4evr0-holdout-response-20260923.json \
  --labeler hyeokjun-holdout-20260923 \
  --sample 0 --primary-only \
  --out /private/tmp/4evr0-holdout-human-20260923.jsonl
```
