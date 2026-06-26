# 2026-06-27 작업 리뷰 (B1 실험: 프롬프트 개선 + v3 승격)

## 오늘 한 일
baseline(v1) 오류를 분석해 프롬프트 v2/v3를 만들고, 같은 정답셋으로 측정해
**"프롬프트 바꿔서 좋아졌나"를 숫자로 비교**(MLflow). 트레이드오프까지 다룬 한 사이클을 끝내고
**v3를 프로덕션 프롬프트로 승격**.

> 측정 기반 개선 루프: **측정 → 오류분석 → 가설 → 실험 → 비교 → (회귀 발견) → 재수정 → 승격**

---

## 1. baseline(v1) 오류 분석

baseline은 concern **precision 0.78 < recall 0.91** → **과잉예측**(필요 없는 concern 추가)이 문제였다.
`eval/results`의 케이스별 gold vs pred를 분석한 결과(FP 11 / FN 4):

| 과잉예측 패턴 | 사례 |
|---------------|------|
| 우산개념 `AGING_SIGNS` | id1, id11 (구체 concern 있는데 추가) |
| `DRY_SKIN` 남발 | id5, id8, id14 (속건조/각질인데 추가) |
| `PORE_CONGESTION` ↔ `ENLARGED_PORES` 혼동 | id2 |
| 색소 그룹 인접 추가(DULLNESS/HYPERPIGMENTATION) | id12 |

→ 가설: **"명시된 것만 뽑고 인접/우산 concern은 빼라" + 헷갈리는 구분 명시** 하면 precision·완전일치 ↑.

## 2. 실험 (같은 정답셋 19개, model=Qwen3.5-9B, temp=0)

| prompt | 변경 | hash |
|--------|------|------|
| v1 | baseline | `5774020c` |
| v2 | 가이드라인 1~5 추가(과잉예측 억제 + skin_type 추론 금지) | `bec341ae` |
| v3 | v2의 가이드 #5를 LABELING 정책에 맞게 완화(직접묘사는 추출) | `7a7bd0e2` |

### 결과 (MLflow `4evr0-profile-extraction`)
| metric | v1 | v2 | v3 |
|--------|:----:|:----:|:----:|
| concern F1 | 0.839 | **0.941** | 0.930 |
| concern precision | 0.780 | **0.952** | 0.930 |
| concern 완전일치 | 0.368 | **0.737** | 0.684 |
| skin_type 정확도 | 0.895 | 0.789 ⚠️ | **0.895** |
| **profile 완전일치** | 0.316 | **0.684** | **0.684** |
| 무효값/에러 | 0/0 | 0/0 | 0/0 |
| 입력 토큰 | 334 | 682 | 783 |

## 3. 핵심 발견 — 트레이드오프와 회귀

- **v1→v2**: 가설 적중. precision +0.17, concern 완전일치 **2배**. recall도 안 깎임(+0.02).
- **그러나 v2는 skin_type −0.11 회귀.** 원인 분석(v2 케이스):
  - 가이드 #5("증상에서 skin_type 추론 금지")가 **과교정** → "건조하다(DRY)", "피지 많다(OILY)"
    같은 **직접 묘사까지 증상으로 보고 skin_type을 비워버림**(id1/2/8/10).
- **v2→v3**: 가이드 #5를 "직접 묘사(건조/피지/번들/복합성)는 추출, 질환·증상(아토피/홍조/따가움)에서만
  추론 금지"로 수정 → **skin_type 0.789 → 0.895 완전 복구.** concern은 v2보다 미세하게만 양보.

## 4. 결정 — v3 승격

- **v3는 v1 대비 모든 지표 ≥** (회귀 없음). v2는 skin_type이 v1보다 나빠 "엄밀히 우월"이 아님.
- profile 완전일치(가장 엄격): v1 0.316 → v3 **0.684** (2.2배).
- 승격 처리:
  - `app/prompts/profile_extraction.txt` ← v3 내용 (production은 `llm_client`가 이 파일 로드)
  - v1 원본은 `profile_extraction.v1.txt` 로 보존, v2/v3도 파일로 lineage 유지.
  - production prompt_version = `7a7bd0e2` (= v3) 확인.
- 비용: 입력 토큰 334→783 (2.3배, 가이드라인 길어진 대가). latency는 ~1.2s로 영향 미미.

## 5. 한계 (정직하게)
- **정답셋 19개·단일 run** — v2 vs v3의 0.01~0.05 차이는 **노이즈일 수 있음**.
  v1→v3의 큰 개선은 확고하나, 미세 비교 확신엔 **정답셋 확대(30~50) + 반복 측정**이 필요.
- v2/v3 프롬프트가 baseline 오류를 보고 작성됨 → 이 데이터셋에 약간 overfit 가능.
  일반 원칙(우산/구분/직접묘사)으로 작성해 완화했으나, **held-out test 분리**가 다음 과제.

## 다음 단계
- 정답셋 확대 + train/dev/test 분리 → v2/v3 우열 재확인
- A1(응답 스트리밍) / C0(응답 품질 평가) — `eval/EXPERIMENTS.md` 참고
- (인프라) ephemeral 키 onstart 자동화로 GPU 콜드스타트 줄이기
