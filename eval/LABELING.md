# 정답셋 라벨링 기준 (eval/dataset.jsonl)

팀이 정답셋을 일관되게 확장하기 위한 라벨링 표준. (2026-06-26 합의)

각 라인 스키마:
```json
{"id": 1, "label": "짧은태그", "message": "사용자 메시지",
 "skin_types": [...], "concerns": [...], "constraints": [...]}
```
- 값은 모두 `app/domain/enums.py` 의 enum 값만 사용 (오타·없는 값 금지).
- `effects` 는 라벨하지 않는다 — `concerns` 에서 자동 추론(`taxonomy_normalization_service.infer_effects`)되므로 평가는 추출이 직접 내는 `skin_types/concerns/constraints` 만 비교한다.

## 1. skin_types — **명시/직접 묘사된 것만** (보수적)

피부의 본질(건성/지성/복합성/민감성/중성)을 **직접 말하거나 직접 묘사**할 때만 라벨.
질환·증상에서 **추론하지 않는다**.

| 인정 (직접) | 라벨 |
|------|------|
| "건성", "건조하고 당겨요" | `DRY` |
| "지성", "피지가 많고", "번들거려요" | `OILY` |
| "복합성", "수부지", "T존 기름 + 볼 건조" | `COMBINATION` |
| "민감성", "예민해요" | `SENSITIVE` |

| 인정 안 함 (증상→추론 금지) | 처리 |
|------|------|
| "아토피가 있어서…" (id10) | skin_type 라벨 X — `ATOPIC_PRONE` 은 concern 으로만. (단 "건조" 명시 시 DRY 는 인정) |
| "홍조가 심해요" (id16) | skin_type 라벨 X — `REDNESS` concern 으로만 |
| "(자극받으면) 따가워요" (id17) | skin_type 라벨 X — `IRRITATED_SKIN` concern 으로만 |

> 이유: 추출 평가가 모델의 **과잉추론(증상→피부타입 비약)**을 패널티 줄 수 있게.

## 2. concerns

- **DRY_SKIN vs DEHYDRATED_SKIN — 표현 단어 그대로**
  - "건조/건성/당김" → `DRY_SKIN`
  - "속건조/수분 부족/수분감 없음" → `DEHYDRATED_SKIN`
- **우산개념은 구체값만** — 구체 concern(`WRINKLES`, `LOSS_OF_ELASTICITY`, `SAGGING_SKIN`)이 있으면
  `AGING_SIGNS` 는 생략. "노화"가 포괄적으로만 언급될 때만 `AGING_SIGNS`.
- 메시지에 나타난 고민만. 추측해서 추가하지 않는다. (네거티브 케이스 id18 = 빈 배열)
- 동의어 매핑은 `taxonomy_normalization_service.py` 의 `_CONCERN_SYNONYMS` 참고
  (예: 블랙헤드→`COMEDONES`, 모공 넓음→`ENLARGED_PORES`, 모공 막힘→`PORE_CONGESTION`).

## 3. constraints — 명시된 것만

`FRAGRANCE_FREE`(향료X) · `ALCOHOL_FREE`(알코올프리) · `VEGAN`(비건) ·
`HYPOALLERGENIC`(저자극) · `EWG_GREEN`(EWG 안전) 중 메시지에 명시된 것만.

## 확장 시
- 현재 50개를 최소 기준으로 유지하고, 새 회귀 유형이 발견될 때 중복되지 않는 케이스를 추가한다.
- 고민 그룹(여드름/유분/민감/건조/장벽/색소/보호/노화)과 제약을 고루 유지한다.
- 회귀 감시용 엣지 케이스 유지: 네거티브(고민 없음), 다중 고민, 오타 유발 표현.
- 추가 후 전체 모델 평가 전에 정적 검증부터 실행한다:
  `python -c "from pathlib import Path; from eval.eval_utils import load_dataset; print(len(load_dataset(Path('eval/dataset.jsonl'))))"`

## 응답 judge의 human 보정 라벨

`run_response_eval.py --human-labels <path>`에 전달하는 JSONL은 전문가가 실제 추천 응답을
동일한 5개 차원으로 평가한 결과다. 모델 점수를 human 라벨로 대체하지 말고, 독립적으로
블라인드 평가한 점수만 기록한다.

```json
{"id": 1, "scores": {"concern_fit": 5, "grounding": 4, "conciseness": 4, "korean_quality": 5, "format_adherence": 4}}
```

- 각 점수는 1~5이며 `id`는 `dataset.jsonl`과 일치해야 한다.
- 최소 10~20개 케이스를 두 명 이상이 독립 평가하고, 합의 점수 또는 평가자 평균을 입력한다.
- 결과 보고서의 `human_calibration`에서 MAE, Pearson, Spearman 상관을 확인한다.
- 상관이 낮은 judge의 절대 점수는 품질 승인 기준으로 사용하지 않는다.
