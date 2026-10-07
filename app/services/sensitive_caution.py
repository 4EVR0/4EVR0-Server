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

from typing import Any, Iterable

from app.domain.enums import Concern

# 민감 계열 중 '사용 가능 성분'으로 해석하는 고민. 아토피·장벽 손상만 요청하면 적용하지 않는다
# (요소처럼 아토피·건조 근거가 있는 성분을 살리기 위해).
SENSITIVE_USE_CONCERNS = frozenset({
    Concern.SENSITIVE_SKIN, Concern.REDNESS, Concern.IRRITATED_SKIN, Concern.ROSACEA_PRONE,
})

RELAXED_NOTE = "민감한 피부라면 저농도·씻어내는 제품부터, 다른 각질 제거 성분과 겹치지 않게 사용"
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
