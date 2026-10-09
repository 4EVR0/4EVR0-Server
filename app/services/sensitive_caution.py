"""민감 피부 주의 표시(그래프 Ingredient.sensitive_caution) 적용 규칙 (GraphRAG_Pipeline #49).

민감 피부 계열 고민은 '민감함을 고치는 성분'이 아니라 '민감한 피부에 써도 되는 성분'을 뜻한다.
파이프라인이 자극 우려 성분을 조치 강도와 함께 노드에 단다.
- sensitive_caution = 'exclude': 민감 계열 요청에서 후보로 쓰지 않는다.
- sensitive_caution = 'caution': 후보로 쓰되 주의 안내를 붙인다.
- sensitive_caution_with: 함께 요청하면 exclude를 caution으로 완화하는 고민
  (예: 살리실산·AHA는 여드름 계열과 함께 요청하면 남긴다).
속성이 없는 그래프(적재 전)에서는 아무것도 바꾸지 않는다.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.domain.enums import Concern

# 민감 계열 중 '사용 가능 성분'으로 해석하는 고민. 아토피·장벽 손상만 요청하면 적용하지 않는다
# (요소처럼 아토피·건조 근거가 있는 성분을 살리기 위해).
SENSITIVE_USE_CONCERNS = frozenset({
    Concern.SENSITIVE_SKIN, Concern.REDNESS, Concern.IRRITATED_SKIN, Concern.ROSACEA_PRONE,
})

RELAXED_NOTE = "민감한 피부라면 저농도·씻어내는 제품부터, 다른 각질 제거 성분과 겹치지 않게 사용"
# 완화로 남긴 성분(각질 제거 산)은 제품 점수에서 하나의 묶음으로 센다. 산을 여러 개 담은 제품이
# 산 개수만큼 점수를 받아 위로 올라오지 않게 한다(안내 문구 '겹치지 않게'와 맞춘다).
RELAXED_SCORE_GROUP = "sensitive_relaxed"
_PEEL_PRODUCT = re.compile(r"필링|peel", re.IGNORECASE)
CAUTION_NOTE = "민감한 피부라면 좁은 부위에 먼저 사용해 확인"


def is_sensitive_use_query(concerns: Iterable[Concern]) -> bool:
    return bool(SENSITIVE_USE_CONCERNS.intersection(concerns))


def _codes(concerns: Iterable[Concern]) -> set[str]:
    return {c.value if isinstance(c, Concern) else str(c) for c in concerns}


def is_relaxed(row: dict[str, Any], concerns: Iterable[Concern]) -> bool:
    """요청 고민이 이 성분의 완화 고민과 겹치는가."""
    return bool(_codes(concerns).intersection(row.get("sensitive_caution_with") or []))


def is_excluded(row: dict[str, Any], concerns: list[Concern]) -> bool:
    """민감 계열 요청에서 이 후보를 뺄지."""
    return (is_sensitive_use_query(concerns) and row.get("sensitive_caution") == "exclude"
            and not is_relaxed(row, concerns))


def caution_note(row: dict[str, Any], concerns: list[Concern]) -> str | None:
    """후보로 남는 성분에 붙일 안내. 민감 계열 요청이 아니면 없다."""
    if not is_sensitive_use_query(concerns):
        return None
    level = row.get("sensitive_caution")
    if level == "exclude" and is_relaxed(row, concerns):
        return RELAXED_NOTE
    if level == "caution":
        return CAUTION_NOTE
    return None


def score_group(row: dict[str, Any], concerns: list[Concern]) -> str | None:
    """제품 점수 묶음. 민감 계열 요청에서 완화로 남긴 exclude 성분이면 하나의 묶음으로 센다."""
    if (is_sensitive_use_query(concerns) and row.get("sensitive_caution") == "exclude"
            and is_relaxed(row, concerns)):
        return RELAXED_SCORE_GROUP
    return None


def is_peel_product(product_name: str | None) -> bool:
    """이름으로 보는 필링(각질 제거) 제품. 민감 계열 요청에서는 후보로 쓰지 않는다."""
    return bool(_PEEL_PRODUCT.search(product_name or ""))


def _topic(word: str) -> str:
    """'살리실릭애씨드는'·'콜레스테롤은'처럼 받침에 맞춘 주제 조사."""
    last = word.strip()[-1:] if word.strip() else ""
    batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return f"{word}{'은' if batchim else '는'}"


def notes_text(items: Iterable[tuple[str, str]]) -> str | None:
    """[(성분 표시 이름, 안내 문구)] → 안내 문구별로 성분을 묶은 '참고:' 줄. 없으면 None.

    응답 본문을 검사한 뒤 서버가 붙이는 문장이다(생성 모델이 안내를 빠뜨려도 항상 나오게).
    """
    grouped: dict[str, list[str]] = {}
    for name, note in items:
        if note and name not in grouped.setdefault(note, []):
            grouped[note].append(name)
    lines = [f"참고: {_topic('·'.join(names))} {note}해 주세요." for note, names in grouped.items() if names]
    return "\n".join(lines) or None
