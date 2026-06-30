# 2026-06-30 · Issue #24 마무리 — 한글 성분명 노출 + 응답 구조/길이 튜닝 + 웹 UI 개편

GitHub Issue [#24] `[UX] 추천 응답에 한글 성분명(kor_name) 노출 + v3 길이/잘림 튜닝`

## 배경 (이슈 요약)
- v3 프롬프트가 영문 INCI 대문자명을 그대로 인용 → `UREA(논문 근거 4건)` 처럼 소비자 응답으로 부자연스러움.
- 원인: `query_ingredients_by_effects`는 `kor_name`을 반환하지만 `IngredientResult` 스키마에 필드가 없어 버려짐.
- 추가로 v3 응답이 길고 가끔 잘림(truncation).

## 완료 기준
- 응답이 한글 성분명으로 근거를 인용하고, 잘림 없이 간결.

---

## 변경 사항

### 1. kor_name 데이터 경로 연결 (제안 1·2)
- `app/schemas/recommend.py` — `IngredientResult`에 `kor_name: str | None` 추가.
- `app/services/recommend_service.py`
  - `IngredientResult` 생성 시 `kor_name=row.get("kor_name")` 보존.
  - `_ingredient_display_name()` 헬퍼 추가 → `한글명(영어명)`, 한글명 없으면 영어명만.
  - LLM 컨텍스트(`_build_llm_response`)와 LLM 실패 폴백 문구에 헬퍼 적용 → **응답 본문도 한글명 사용**.
  - `evidence_by_name` 매핑은 제품 매칭 성분(영문 inci)과 키를 맞춰야 하므로 영문 `i.name` 키 유지.

### 2. 응답 구조 변경 — 성분 설명 → 제품 추천 (사용자 요청)
- 새 프롬프트 `app/prompts/recommend_response.v4.txt` 작성, 프로덕션 기본을 v3 → v4로 교체.
- Format 순서: ① 고민 분석 → ② **성분 설명**(각 성분이 어떤 문제에 어떻게 효과, 근거 수준, `한글명(영어명)` 사용) → ③ **제품 추천**(제공 목록 한정) → ④ 사용 팁.
- v3에서 judge eval로 튜닝된 STRICT grounding 규칙은 그대로 유지.

### 3. 길이/잘림 튜닝 (제안 3)
- `app/core/config.py` — `gen_max_tokens: int = 1024` 추가(`GEN_MAX_TOKENS`로 조정 가능).
- `recommend_service`의 LLM 호출에 `max_tokens=settings.gen_max_tokens` 전달 → 잘림 방지 헤드룸 확보.
- 간결성은 v4 프롬프트의 "Keep the whole answer concise and practical." 지시로 보강.

### 4. judge 컨텍스트 공정성 (eval)
- `eval/run_response_eval.py` — generator와 동일하게 성분 라인을 `_ingredient_display_name`(`한글명(영어명)`)로 표기.
- `ev`/`_annotate` 매핑은 영문 inci 키 유지(제품 핵심성분과 정합).

### 5. 웹 UI 개편 (`app/static/index.html`)
- 다크 네이비 → **연한 초록 테마**(CSS 변수 팔레트화).
- **마크다운 렌더러 추가**(의존성 없음, HTML escape 후 변환 → XSS 안전): 헤더/볼드/이탤릭/리스트/인용/코드블록/링크/수평선.
- 성분 카드에 `한글명(영어명)` 표기(영어명은 옅은 보조 텍스트).
- 챗봇 느낌 강화(버블 그림자·비대칭 모서리, 타이핑 인디케이터 등).

---

## 검증
- Python 문법/임포트 체크 통과(config, 서비스 헬퍼, eval 임포트, v4 프롬프트 로드 hash `188774cb`).
- `_ingredient_display_name` 동작 확인: `우레아(UREA)`, 한글명 없으면 `UREA`.
- 마크다운 렌더러 단위 동작 확인(헤더/리스트/볼드/코드/이스케이프).
- **미검증**: 실제 LLM 응답 품질(모델 서버 필요). v4를 `eval/run_response_eval.py`로 돌려 v3 대비 grounding/conciseness 점수 비교 권장.

## 배포 메모
- UI(`index.html`): 서버 재시작 불필요, 브라우저 강력 새로고침(Cmd+Shift+R).
- 스키마/서비스/프롬프트/config: 서버 재시작 필요(`uvicorn`에 `--reload` 없음, 프롬프트는 import 시 1회 로드 + lru_cache).

[#24]: https://github.com/4EVR0/4EVR0-Server/issues/24
