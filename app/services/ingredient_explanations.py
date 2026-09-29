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

# 이 추가 근거가 있을 때만 기존 '논문 성분 우선' 지침을 보완한다.
GENERATION_POLICY = """
검토된 보습 성분 설명이 제공된 이번 요청에는 다음 근거 사용 규칙을 우선 적용하세요.
- 제품 함유가 확인된 '일반 성분 역할 근거'도 보습 추천 이유로 사용하세요. 논문 건수나 검색 핵심 성분만으로 설명을 제한하지 마세요.
- '성분 설명'에 제공된 보습 설명 성분을 최소 하나 포함하고, 무엇을 돕는지와 제공된 작용 이유를 짧게 설명하세요. 구체적인 작용 이유가 제공된 성분을 우선하세요.
- 추천할 제품에 그 성분이 확인되면, 그 제품의 설명에도 성분의 일반 역할과 사용자의 건조함을 연결한 이유를 한 문장으로 포함하세요.
- 일반 성분 역할은 논문 임상 결과나 완제품 효과의 입증이 아닙니다. 함량·효과의 크기·우열·시너지·저자극·깊은 침투를 추정하지 마세요.
- 효능 라벨과 논문 건수만 제공된 다른 성분에는 구체적인 작용기전을 새로 붙이지 마세요. 피부 장벽 손상 등 사용자 상태의 원인도 확정하지 마세요.
- 소비자에게는 간결한 한국어 설명을 우선하세요. 책 제목·쪽수와 논문 건수는 본문에 반복하지 않아도 됩니다. 제품명·함유 근거·길이 제한은 유지하세요.
""".strip()
POLICY_SHA256 = hashlib.sha256(GENERATION_POLICY.encode("utf-8")).hexdigest()


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


def generation_system_prompt(base: str, products: list[Any]) -> str:
    """카드 미제공/OFF에서는 기존 시스템 프롬프트를 한 글자도 바꾸지 않는다."""
    if not any(product_explanations(product) for product in products):
        return base
    return f"{base}\n\n{GENERATION_POLICY}"
