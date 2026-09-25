"""원문을 별도 확인한 성분 연구. 그래프의 논문 건수와 출처를 혼동하지 않는다."""

import json
from pathlib import Path

from app.schemas.recommend import IngredientResult

_PATH = Path(__file__).resolve().parent.parent / "data" / "verified_ingredient_studies.json"
_STUDIES = {
    (entry["inci_name"].upper(), entry["effect_claim"].casefold()): entry
    for entry in json.loads(_PATH.read_text(encoding="utf-8"))
}


def verified_study_for(ingredient: IngredientResult) -> dict | None:
    """성분 INCI와 조회된 효능이 모두 일치할 때만 별도 연구를 반환한다."""
    return _STUDIES.get((ingredient.name.upper(), (ingredient.claim or "").casefold()))


def render_verified_studies(
    ingredients: list[IngredientResult], response_text: str | None = None,
) -> str:
    """검증 템플릿 평가자가 동일한 참고 연구 조건·한계를 보도록 조립한다."""
    lines = []
    seen = set()
    for ingredient in ingredients[:10]:
        study = verified_study_for(ingredient)
        if study is None or study["pmid"] in seen:
            continue
        if response_text is not None and study["url"] not in response_text:
            continue
        seen.add(study["pmid"])
        lines.append(
            f"- {ingredient.name}: {study['summary_ko']} "
            f"한계: {study['limitation_ko']} 출처: {study['url']} "
            "(별도 검토 연구이며 그래프 논문 건수에 포함됐는지는 확인되지 않음)"
        )
    return "\n".join(lines) or "(없음)"
