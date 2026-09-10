# 채점 루브릭 수정 — 거절/날조 구분 + 한자 검사 결정화

- 날짜: 2026-09-11
- 브랜치: `fix/eval-session-isolation` (커밋 `17ed8eb`)
- 배경: `review/2026-09-11-v7-baseline.md` 2장 — judge가 정직한 거절과 제품 날조에 같은 1점
- 최종 기준선: `eval/results/v7-baseline-rubric2.json` (run_id `20260911-044021-366375`)

## 1. 고친 것 — grounding에 "추천 0건" 정의 추가

`response_judge.txt`의 grounding은 *"추천 제품이 제공 데이터에 있나"* 만 규정하고
**추천이 0건일 때를 정의하지 않았다.** judge는 이를 "grounded된 추천 없음 = 최저점"으로
해석해, 데이터가 없어 거절한 응답에 1점을 줬다.

추가한 규정:

```
When [Provided products] is "(없음)" — no products were retrieved:
  Declining to name products is the CORRECT behaviour, not a defect.
  - grounding: 5 if the response names NO specific product …
    Naming ANY specific brand or product here is fabrication — score 1.
  - format_adherence / concern_fit: Do NOT deduct merely because a product section is absent.
  Never reward a response for inventing products to fill the gap.
```

옛 루브릭은 `response_judge.v1.txt`로 동결했다(해시 `4603be1a` 보존).

## 2. 측정 방법 — 재채점으로 효과를 분리

루브릭을 고친 뒤 전체 평가를 다시 돌리면 **생성 비결정성이 섞인다.** vLLM은
`temperature=0`에서도 배치·커널 비결정성 때문에 완전히 결정적이지 않다(실제로
id 20의 날조가 실행마다 달라졌다). 그러면 점수 변화가 루브릭 때문인지 응답이 달라져서인지
구분할 수 없다.

그래서 `eval/rejudge.py`를 만들었다. 리포트에 저장된 `response`·`evidence`를 그대로
다시 채점하므로 **입력이 동일하고 차이는 루브릭 효과뿐**이다. GPU·Neo4j가 필요 없다.

```bash
python eval/rejudge.py --report <report>.json --judge-repeats 3 --out <rejudged>.json
python eval/rejudge.py --compare <report>.json <rejudged>.json
```

### 동일 응답 50건 재채점 결과

| 차원 | v1 루브릭 | v3 루브릭 | 차이 |
|---|---|---|---|
| concern_fit | 4.800 | 4.987 | +0.187 |
| **grounding** | 4.513 | **4.940** | **+0.427** |
| conciseness | 3.860 | 4.020 | +0.160 |
| korean_quality | 4.813 | 4.960 | +0.147 |
| format_adherence | 4.607 | 5.000 | +0.393 |

검색 공백 5건의 grounding이 **전부 1 → 5**로 바뀌었다. 점수가 변한 케이스는 12건으로,
공백 케이스 외에는 변동이 작았다 — 루브릭 수정이 의도한 지점에만 작용했다는 뜻이다.

## 3. 실패한 시도 — 한자 감점을 루브릭에 넣기

같이 시도했던 변경이 하나 더 있다. `korean_quality` 정의에 "한자(漢字)를 감점하라"를
명시했다(`a150f19b`). **결과가 정확히 반대로 나왔다.**

| 그룹 | korean_quality 변화 |
|---|---|
| 실제로 한자가 누출된 3건 | **+0.333** (id 47은 3→4로 상승) |
| 한자가 없는 47건 | **−0.163** (14건 하락) |

judge는 한자를 신뢰성 있게 못 잡는다. 지시를 추가하자 엉뚱한 케이스에서 더 깐깐해졌을 뿐이다.
해당 조항을 **되돌리고**(최종 `2e1ea732`), 정규식 검사로 옮겼다.

```python
HANJA_PATTERN = re.compile(r"[一-鿿]")   # metrics: hanja_leak_cases / hanja_leak_rate
```

### 이 판단을 뒷받침하는 직접 증거

최종 기준선에서 한자가 누출된 4건에 judge가 매긴 `korean_quality`:

| id | 누출 문자 | judge의 korean_quality |
|---|---|---|
| 4 | 您, 求, 的, 需 | **5** |
| 15 | 肤 | **5** |
| 47 | 牢 | 4 |
| 48 | 修, 护 | 4 |

**중국어 4글자가 섞인 응답에 judge가 만점을 줬다.** 기계적으로 판정 가능한 속성을
LLM에 맡기면 안 된다는 근거가 된다.

## 4. 최종 기준선 (루브릭 `2e1ea732`)

```
generator=cyankiwi/Qwen3.5-9B-AWQ-4bit   judge=gpt-4o-mini
gen_prompt=47f37c55(v7)   judge_prompt=2e1ea732   session_mode=isolated
```

| 지표 | 값 |
|---|---|
| concern_fit | 4.96 (95% CI 4.90–5.00) |
| grounding | 4.86 (4.68–4.98) |
| conciseness | 4.01 (3.93–4.08) |
| korean_quality | 4.91 (4.82–4.98) |
| format_adherence | 4.95 (4.87–5.00) |
| **OVERALL** | **4.736** (4.66–4.80) |
| 에러율 | 0.0 |
| 응답생성 p50 | 4.43s |
| **한자 누출** | **4건 / 8%** |
| 오염 케이스 | 0 |
| judge 반복 표준편차 | 0.0057 |

검색 공백 5건은 **전부 올바르게 거절**했다(grounding 5). 이번 실행에서는 날조가 없었다.

`grounding < 5`인 4건은 모두 제품이 있는 케이스다.

| id | grounding | judge 코멘트 |
|---|---|---|
| 3 | 2 | 제품 추천이 잘못된 정보에 기반하고 있습니다. |
| 21 | 3 | 추천 제품 중 일부는 제공된 데이터에 없는 성분을 포함하고 있습니다. |
| 14 | 4 | 각질 제거 성분이 부족하고, 제품 설명이 다소 길어. |
| 36 | 4 | 일부 제품의 효과에 대한 근거가 부족함. |

**이제 남은 grounding 실패가 "거절 오판"이 아니라 실제 근거 문제다.** 다음 조사 대상.

### 재채점본과 신규 실행의 차이

같은 루브릭인데 재채점본은 OVERALL 4.781, 신규 실행은 4.736이다. 응답이 달라졌기
때문이며(생성 비결정성), **루브릭 비교에 재채점을 쓴 이유를 그대로 보여준다.**
한자 누출도 3건 → 4건으로 달라졌다.

## 5. judge 반복 표준편차 주의 (재확인)

`judge_repeat_stddev = 0.0057`이지만 judge temperature가 0이므로 이는 **결정성**이지
정확성이 아니다. 신뢰 근거로 인용하지 않는다. 타당성은 사람 라벨 비교로만 얻는다.

## 6. 다음

1. **사람 블라인드 라벨링 40건** — 대상 리포트: `v7-baseline-rubric2.json`
   (루브릭이 확정됐으므로 이제 라벨이 헛돌지 않는다)
2. judge-vs-human MAE·Pearson·Spearman
3. 세션 격리 A/B (`--session-mode shared`)
4. 코드 대응: 한자 누출 8% 차단, `grounding<5` 4건 원인 조사, 검색 공백 처리

## 7. 면접 답변으로 쓸 수 있는 형태

> 루브릭을 두 군데 고쳤는데, 하나는 성공하고 하나는 실패했습니다.
>
> 성공한 쪽은 근거성 정의였습니다. "추천이 0건일 때"가 규정돼 있지 않아서 judge가
> 정직한 거절을 날조와 같은 1점으로 채점하고 있었습니다. 규정을 추가하니 해당 5건이
> 1점에서 5점으로 정정됐고, 전체 근거성이 4.51에서 4.94로 올랐습니다.
>
> 실패한 쪽은 한자 누출이었습니다. 루브릭에 감점 조항을 넣었더니 **정작 한자가 섞인
> 케이스는 점수가 오르고 멀쩡한 케이스가 내려갔습니다.** 실제로 중국어 네 글자가 들어간
> 응답에 judge가 만점을 줬습니다. 그래서 되돌리고 정규식 검사로 옮겼습니다.
> 기계적으로 판정 가능한 건 LLM에 맡기지 않는다는 기준을 그때 세웠습니다.
>
> 두 변경 모두 저장된 응답을 재채점해서 측정했습니다. 전체를 다시 돌리면 vLLM이
> temperature 0에서도 완전히 결정적이지 않아서 루브릭 효과와 생성 변동이 섞이거든요.
