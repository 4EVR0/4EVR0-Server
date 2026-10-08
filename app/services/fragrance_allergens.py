"""착향제 알레르기 유발 성분 표시 안내(GraphRAG_Pipeline #49 후속).

추천은 그대로 두고, 추천 제품의 전성분(CONTAINS)에 식약처가 표시를 정한 착향제 알레르기 유발
성분 25종이 있으면 응답 끝에 사실만 안내한다. 이 표시는 위험 물질 경고가 아니라 전성분 표시
범위를 넓힌 것이므로 "표시돼 있다"고만 말하고 전성분 확인을 권한다.

안내 문구는 서버가 만들어 출력 가드 검사가 끝난 뒤 붙인다. 생성 문장에 리날룰 등이 나오면
민감 계열 출력 가드가 응답을 교체하므로, 이 문구를 LLM에 맡기지 않는다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

_PATH = Path(__file__).resolve().parent.parent / "data" / "fragrance_allergens.json"


@lru_cache(maxsize=1)
def allergen_names() -> dict[str, str]:
    """INCI(대문자) → 규정의 한글 성분명. 별칭 INCI도 같은 이름으로 잡는다."""
    data = json.loads(_PATH.read_text(encoding="utf-8"))
    names = {row["inci"].upper(): row["kor"] for row in data["ingredients"]}
    for alias, inci in data.get("aliases", {}).items():
        names[alias.upper()] = names[inci.upper()]
    return names


def allergens_in(inventory: Iterable[dict[str, Any]]) -> list[str]:
    """제품 전성분 행({"name": INCI, ...})에서 알레르기 유발 성분의 한글명(규정 순서 아님, 이름순·중복 없음)."""
    names = allergen_names()
    return sorted({names[str(row.get("name") or "").strip().upper()]
                   for row in inventory if str(row.get("name") or "").strip().upper() in names})


def allergen_note(products: Iterable[tuple[str, list[str]]]) -> str | None:
    """[(제품 표시 이름, 알레르기 성분 한글명 목록)] → 제품마다 한 줄 안내. 해당 제품이 없으면 None."""
    lines = [
        f"참고: {name} 제품에는 향료 알레르기 유발 가능 성분({'·'.join(found)})이 표시돼 있어요. "
        "향료에 민감하다면 제품 상세 정보의 전성분을 꼭 확인해 주세요."
        for name, found in products if found
    ]
    return "\n".join(lines) or None
