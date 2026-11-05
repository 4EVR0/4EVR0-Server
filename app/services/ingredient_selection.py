"""추천 성분 최종 선정 (docs/HANDOFF_regulation_and_evidence.md 6장).

입력은 query_ingredients_by_effects 순서(고민별 논문 근거 → 논문 근거 우선, graph_score 내림차순)의 후보 행과
선정된 제품 행이다. 국내 금지·CAUTION 필터는 이미 적용된 상태를 전제로 한다.

규칙
- 선정 자격: 전체 1위와 같은 근거 등급이면서 점수가 1위 × score_ratio 이상. 1위는 항상 선정(최소 1).
- 자격 있는 후보를 효능(claim)별로 묶어 효능마다 1개씩 돌아가며 고르고, 효능당 최대 2개
  (효능이 1개면 제한 없음). 약한 효능 그룹이 자격 없이 끼어들지 않게 효능 배분은 다양성 용도로만 쓴다.
- 최종 개수: 기본 default_k, 자격 있는 효능이 더 많으면 그 수만큼, 최대 max_k.
- 같은 계열(히알루론산류, 비타민C류, 세라마이드류 등)은 1개만.
- 선정된 제품에 매칭된 성분은 들어 있는 제품 비율만큼 가점: graph_score × (1 + product_bonus × 함유 제품 수 / 제품 수).
  (한 제품에만 있는 성분이 모든 제품에 있는 성분과 같은 가점을 받지 않게 한다.)
- 고민이 둘 이상이면 고민마다 근거 성분이 최소 1개 들어가게 한다. 위 규칙으로 고른 성분이 어떤 고민에도
  연결되지 않으면, 그 고민의 후보 중 가장 앞선 성분을 더한다(최대 개수를 넘으면 다른 고민이 이미 다른 성분으로
  채워진 성분과 바꾼다). 근거 등급이 높은 한 고민의 성분만 남아 다른 고민이 통째로 빠지는 것을 막는다.
  후보 행의 "concerns"(그 성분이 후보로 나온 고민 코드)를 쓴다.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (family, re.compile(pattern))
    for family, pattern in (
        ("hyaluronic", r"HYALURON"),
        ("vitamin_c", r"ASCORB"),
        ("ceramide", r"CERAMIDE"),
        ("retinoid", r"RETIN"),
        ("centella", r"CENTELLA|MADECASS|ASIATIC"),
        ("panthenol", r"PANTHEN"),
        ("salicylate", r"SALICYL"),
        ("tocopherol", r"TOCOPHER"),
        ("glucan", r"GLUCAN"),
        ("peptide", r"PEPTIDE"),
    )
)
_SALT_PREFIX = re.compile(r"^(SODIUM|POTASSIUM|MAGNESIUM|CALCIUM|ZINC|DISODIUM)\s+")


def ingredient_family(name: str) -> str:
    """같은 계열로 볼 성분 키. 목록에 없으면 염 접두어만 뗀 이름."""
    upper = " ".join(str(name).upper().split())
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(upper):
            return family
    return _SALT_PREFIX.sub("", upper)


# 고민별 논문 근거(pubmed_review, #49)는 질환이 맞는 사람 대상 연구만 센 근거라 논문 효능 근거보다 앞선다.
_TIER = {"pubmed_review": 0, "pubmed_evidence": 1}


def _tier(row: dict[str, Any]) -> int:
    return _TIER.get(str(row.get("eligibility_tier") or ""), 2)


def select_recommended_ingredients(
    rows: Sequence[dict[str, Any]],
    products: Sequence[dict[str, Any]],
    *,
    default_k: int,
    max_k: int,
    score_ratio: float,
    product_bonus: float,
    concerns: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """후보 행에서 최종 추천 성분 행을 고른다. 반환 순서는 선정 순서(효능 커버리지 우선)."""
    if not rows:
        return []
    coverage: dict[str, int] = {}
    for product in products:
        for name in set(product.get("matched_ingredients") or []):
            coverage[str(name)] = coverage.get(str(name), 0) + 1
    n_products = len(products) or 1

    scored = []
    for index, row in enumerate(rows):
        score = float(row.get("graph_score") or 0.0)
        score *= 1.0 + product_bonus * coverage.get(str(row.get("name")), 0) / n_products
        scored.append((_tier(row), -score, index, row))
    scored.sort()
    top_tier, top_score = scored[0][0], -scored[0][1]
    eligible = [item for item in scored if item[0] == top_tier and -item[1] >= top_score * score_ratio]

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in eligible:
        groups.setdefault(str(item[3].get("claim") or ""), []).append(item[3])
    ordered = list(groups.values())  # 그룹 순서 = 그룹 1위 점수 순

    k = min(max_k, max(default_k, len(ordered)))
    per_group_cap = 2 if len(ordered) > 1 else k
    chosen: list[dict[str, Any]] = []
    families: set[str] = set()
    taken = [0] * len(ordered)
    progressed = True
    while len(chosen) < k and progressed:
        progressed = False
        for g, group in enumerate(ordered):
            if len(chosen) >= k or taken[g] >= per_group_cap:
                continue
            for row in group:
                family = ingredient_family(row.get("name", ""))
                if any(row is picked for picked in chosen) or family in families:
                    continue
                chosen.append(row)
                families.add(family)
                taken[g] += 1
                progressed = True
                break
    if len(concerns) > 1:
        _cover_every_concern(chosen, families, [item[3] for item in scored], list(concerns), max_k)
    return chosen


def _serves(row: dict[str, Any], code: str) -> bool:
    return code in (row.get("concerns") or [])


def _cover_every_concern(chosen: list[dict[str, Any]], families: set[str],
                         ranked: list[dict[str, Any]], concerns: list[str], max_k: int) -> None:
    """고민마다 선정 성분이 최소 1개 있게 chosen을 고친다. 후보가 없는 고민은 그대로 둔다."""
    for code in concerns:
        if any(_serves(row, code) for row in chosen):
            continue
        candidate = next((row for row in ranked if _serves(row, code)
                          and not any(row is picked for picked in chosen)
                          and ingredient_family(row.get("name", "")) not in families), None)
        if candidate is None:
            continue
        if len(chosen) >= max_k:
            # 빼도 요청 고민이 모두 다른 성분으로 남는 성분 중 가장 뒤의 것과 바꾼다.
            for i in range(len(chosen) - 1, -1, -1):
                others = chosen[:i] + chosen[i + 1:]
                own = [c for c in concerns if _serves(chosen[i], c)]
                if all(any(_serves(row, c) for row in others) for c in own):
                    families.discard(ingredient_family(chosen[i].get("name", "")))
                    del chosen[i]
                    break
            else:
                continue
        chosen.append(candidate)
        families.add(ingredient_family(candidate.get("name", "")))
