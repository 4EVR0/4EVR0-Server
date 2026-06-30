# 2026-06-30 · Issue #24 통합 — 한글 성분명 + 성분설명→제품추천 구조 + 연녹 챗봇 UI

GitHub Issue #24 `[UX] 추천 응답에 한글 성분명(kor_name) 노출 + v3 길이/잘림 튜닝`

이슈 #24를 독립적으로 해결한 두 브랜치를 하나로 통합한 결과.

- **A** `feat/issue-24-korean-ingredient-ux` — 견고한 한글명 처리 + 길이/잘림 규율 + 테스트
- **B** `feat/issue-24-kor-name-ux` — 성분 설명→제품 추천 구조 + 연녹 챗봇 UI(마크다운)

## 통합 방침
A를 베이스로 두고 B의 강점을 얹음. 백엔드는 A(더 견고 + 테스트 보유), 응답 구조와 웹 UI는 B.

## 통합 결과 (채택 출처)
- `schemas/recommend.py` — `kor_name` 필드 (A=B 동일)
- `core/config.py` — `gen_max_tokens=1200` (A; 성분 설명 섹션 추가로 더 긴 응답 대비)
- `services/recommend_service.py` — **A**: 견고한 `_ingredient_display_name`(공백 strip + `kor==inci` 시 괄호 생략) + 제품 핵심성분도 한글명/근거로 표기
- `eval/run_response_eval.py` — **A**: judge 컨텍스트도 동일 한글명 표기
- `app/prompts/recommend_response.v4.txt` — **병합**: A의 한글명 우선·길이/완결 규율(at most 3 products, 한글명 (INCI), "Never end with a dangling") 유지 + **B의 "성분 설명 → 제품 추천" 순서** 삽입. 길이 예산 700→900자(성분 설명 분량 반영)
- `app/static/index.html` — **B**: 연녹 테마 + 의존성 없는 마크다운 렌더러(escape 후 변환, XSS 안전). 성분 카드는 A의 `.trim()`/중복(kor==inci) 처리 방식으로 정렬
- `tests/test_korean_ingredient_response.py` — **A**: 헬퍼·파이프라인·judge·웹카드 검증 7개 유지
- `.env.example` — A 갱신 유지

## 검증
- `pytest tests/test_korean_ingredient_response.py` → **7 passed**
- 병합 v4 프롬프트가 테스트 검증 문구(at most 3 products / 한글명 (INCI) / Never end with a dangling)를 모두 보존함을 테스트로 확인
- 웹 카드 표기식이 `escHtml(ing.kor_name.trim())` 형태로 테스트와 일치
- ⚠️ 실제 LLM 응답 품질(정량 judge eval)은 외부 judge 키 미설정으로 미실행

## 정리
- 통합 브랜치: `feat/issue-24-integrated`
- 기존 A/B 브랜치 및 PR은 통합본으로 대체 → 닫기 권장
