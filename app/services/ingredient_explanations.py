"""검토된 성분 설명. 효능 검색·순위 점수와 별개인 제품 함유 보조 근거."""

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from app.domain.enums import Concern
from app.schemas.recommend import ProductIngredientExplanation

_PATH = Path(__file__).resolve().parent.parent / "data" / "ingredient_explanation_cards.json"
_RAW = _PATH.read_bytes()
CARD_SHA256 = hashlib.sha256(_RAW).hexdigest()
_DOCUMENT = json.loads(_RAW)
_CARDS = tuple(card for card in _DOCUMENT["cards"] if card["review_status"] == "reviewed_paraphrase")


def _field(row: Any, name: str, default=None):
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def cards_for_inventory(inventory: list[dict], concerns: list[Concern]) -> list[ProductIngredientExplanation]:
    """정규 INCI 완전 일치만 사용하며, 히알루론산 일반명에서 염을 추측하지 않는다."""
    names = {str(row.get("name") or "").strip().upper() for row in inventory}
    concern_names = {concern.value for concern in concerns}
    return [ProductIngredientExplanation.model_validate(card) for card in _CARDS
            if card["name"] in names and concern_names.intersection(card["allowed_concerns"])]


def supports_concerns(concerns: list[Concern]) -> bool:
    names = {concern.value for concern in concerns}
    return any(names.intersection(card["allowed_concerns"]) for card in _CARDS)


def product_explanations(product: Any) -> list[ProductIngredientExplanation]:
    """Serving/eval 모두 저장된 카드의 검토 버전과 내용을 확인한다.

    설정을 읽지 않아 OFF 상태에서도 과거 ON 결과를 동일 근거로 평가할 수 있다.
    """
    allowed = {card["name"]: ProductIngredientExplanation.model_validate(card) for card in _CARDS}
    result = []
    seen = set()
    for row in _field(product, "ingredient_explanations", []) or []:
        try:
            card = ProductIngredientExplanation.model_validate(row)
        except (TypeError, ValueError):
            continue
        if card == allowed.get(card.name) and card.name not in seen:
            result.append(card)
            seen.add(card.name)
    return result


def render_product_explanations(product: Any) -> str:
    cards = product_explanations(product)
    if not cards:
        return ""
    lines = [f"  · 제품 함유 확인 + 검토된 일반 성분 설명: {card.kor_name} ({card.name}) [일반 성분 역할 근거]: {card.explanation}"
             for card in cards]
    lines.append("  · 건조·보습 추천 이유에 이 일반 역할 설명을 활용하세요. 논문 근거 건수와 구분한 보조 설명이며 제품 임상 효과, 저자극, 알레르기 안전성, 피부 깊은 침투를 입증하지 않습니다. 책 제목·쪽수는 추천 본문에 반복하지 마세요.")
    return "\n".join(lines)
