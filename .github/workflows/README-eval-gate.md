# eval-gate — 품질 회귀 게이트 (이슈 #41)

PR의 프롬프트·추출·생성·그래프 변경에서 **검증된 지표가 나빠지면 머지를 차단**한다.
사람 교정을 통과하지 못한 LLM Judge 의미 점수는 관측만 하고 통과 여부에는 쓰지 않는다.

## 동작

```
PR에 `run-eval` 라벨  ─┐
수동 dispatch         ─┴─▶ self-hosted 러너(Mac)
                           → run_eval.py (추출) + run_response_eval.py (생성/결정론 검사)
                             (Tailscale로 vLLM·Neo4j, 로컬 pg/redis, 캐시 off)
                           → check_gate.py: 임계 비교
                           → PR 코멘트에 점수 표
                           → 미달이면 job 실패 → 머지 차단
```

**왜 라벨/수동인가:** GPU(vLLM)가 vast.ai에서 **세션마다 대여·destroy**되는 ephemeral 자원이라,
GitHub 클라우드 러너는 닿을 수 없고 항상 떠있지도 않다. → **GPU가 떠있을 때** 라벨을 붙여 실행한다.
preflight가 vLLM 미도달/모델 불일치를 먼저 잡아준다.

## 게이트 임계값 (`eval/gate_config.json`)

추출·검색은 기준선 아래로 마진을 둔다. 응답은 사람 교정에 실패한 의미 점수를 제외하고
기계적으로 판정 가능한 무결성 지표만 사용한다.

| 섹션 | 지표 | 기준 |
|---|---|---|
| 추출 | concern F1 | ≥ 0.83 |
| 추출 | skin_type 정확도 | ≥ 0.94 |
| 추출 | 무효값/에러율 | ≤ 0.02 / 0 |
| 생성 | 에러율 | 0 |
| 생성 | 한자 누출률 | 0 |
| 생성 | 세션 오염률 | 0 |
| 검색 | 제품 precision | ≥ 0.75 |
| 검색 | 제품 0-결과율 | ≤ 0.15 |
| 검색 | 성분 precision | ≥ 0.40 |

2026-09-22 사람 블라인드 40건에서 핵심 3축 케이스 평균 Pearson은 `0.4835`로
방향성은 있었지만, Judge가 평균 `+0.753` 과대평가했다. `judge OVERALL`·`grounding` 등
의미 점수는 새 루브릭의 절대점수 편향이 사람 holdout에서 허용 범위에 들어온 뒤에만
게이트로 복귀시킨다. 간결성·형식 준수는 주 판정이 아닌 보조 진단값으로 유지한다.

> 임계 조정은 `eval/gate_config.json`만 고치면 됨. 로컬 검증: `python eval/check_gate.py --extraction <j> --response <j>`.

## 최초 세팅 (1회)

### 1. self-hosted 러너 등록 (Mac)
개발 Mac(Tailscale로 GPU·Neo4j 접근 가능, docker로 pg/redis 구동 중)에 러너를 붙인다:

1. GitHub → repo **Settings → Actions → Runners → New self-hosted runner** (macOS)
2. 안내대로 `./config.sh --url https://github.com/4EVR0/4EVR0-Server --token <T>` 실행
3. 상시 실행하려면 `./svc.sh install && ./svc.sh start` (서비스 등록), 또는 세션 중 `./run.sh` 수동.
4. 러너 머신에 필요: `python3`, `pip`, `gh`(auth 불필요 — 워크플로우가 GITHUB_TOKEN 사용), Tailscale up, 로컬 pg/redis(5432/6379).

### 2. Secrets / Variables 등록
repo **Settings → Secrets and variables → Actions**:

| 종류 | 이름 | 값 |
|---|---|---|
| Secret | `JUDGE_API_KEY` | OpenAI 키 (judge=gpt-4o-mini) |
| Secret | `GPU_SERVER_URL` | `http://vast-gpu-server-2.tailb70036.ts.net:18000` |
| Secret | `NEO4J_URI` | `bolt://ip-172-31-56-102.tailb70036.ts.net:7687` |
| Secret | `NEO4J_PASSWORD` | (Neo4j 비번) |
| Variable(선택) | `GPU_MODEL` | 기본 `cyankiwi/Qwen3.5-9B-AWQ-4bit` |
| Variable(선택) | `JUDGE_MODEL` | 기본 `gpt-4o-mini` |

> pg/redis/neo4j_user 기본값은 워크플로우에 내장(로컬 docker 기준). 다르면 Variable로 덮어쓰기.

### 3. `run-eval` 라벨 생성 + 브랜치 보호
- repo Labels에 **`run-eval`** 추가.
- **Settings → Branches → main 보호 규칙**: "Require status checks" 에 `eval-gate / eval-gate` 추가
  → 게이트 실패 시 머지 버튼 잠김. (이게 있어야 "차단"이 강제됨)

## 사용

1. **GPU를 띄운다** (vast.ai, `VLLM_MODEL`=채택 모델). vLLM ready 확인.
2. 검증할 PR에 **`run-eval` 라벨**을 붙인다. (또는 Actions 탭 → eval-gate → Run workflow, `pr` 입력)
3. ~수십 분 후 PR에 점수 표 코멘트 + 체크 결과. 미달이면 머지 차단.
4. 빠른 확인은 dispatch의 `limit`(예: 10)로 표본 축소.

## 향후 (이슈 #41 C4)
- #39(가드레일)·#40(검색 eval)의 지표가 생기면 `gate_config.json`에 섹션 추가 → 단일 품질 CI로 통합.
- 경로 필터 자동 라벨링(프롬프트/서비스 건드린 PR에 자동 `run-eval`)은 확장 과제.
