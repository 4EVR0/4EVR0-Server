# eval-gate — 품질 회귀 게이트 (이슈 #41)

PR의 프롬프트·추출·생성·그래프 변경이 **품질을 떨어뜨리면 머지를 차단**한다.
유닛테스트가 "버그=머지 금지"라면 이건 "**품질 저하=머지 금지**".

## 동작

```
PR에 `run-eval` 라벨  ─┐
수동 dispatch         ─┴─▶ self-hosted 러너(Mac)
                           → run_eval.py (추출) + run_response_eval.py (생성 judge)
                             (Tailscale로 vLLM·Neo4j, 로컬 pg/redis, 캐시 off)
                           → check_gate.py: 임계 비교
                           → PR 코멘트에 점수 표
                           → 미달이면 job 실패 → 머지 차단
```

**왜 라벨/수동인가:** GPU(vLLM)가 vast.ai에서 **세션마다 대여·destroy**되는 ephemeral 자원이라,
GitHub 클라우드 러너는 닿을 수 없고 항상 떠있지도 않다. → **GPU가 떠있을 때** 라벨을 붙여 실행한다.
preflight가 vLLM 미도달/모델 불일치를 먼저 잡아준다.

## 게이트 임계값 (`eval/gate_config.json`)

채택 모델(AWQ int4) 실측 아래로 마진을 두고, P3 v5 붕괴는 차단하도록 보정:

| 섹션 | 지표 | 기준 | AWQ(채택) | v5(회귀) |
|---|---|---|---|---|
| 추출 | concern F1 | ≥ 0.83 | 0.841 ✅ | — |
| 추출 | skin_type 정확도 | ≥ 0.94 | 0.96 ✅ | — |
| 추출 | 무효값/에러율 | ≤ 0.02 / 0 | 0 / 0 ✅ | — |
| 생성 | judge OVERALL | ≥ 4.40 | 4.58 ✅ | 4.04 ❌ |
| 생성 | grounding | ≥ 4.30 | 4.52 ✅ | 3.80 ❌ |
| 생성 | format 준수 | ≥ 4.30 | 4.78 ✅ | 3.85 ❌ |

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
