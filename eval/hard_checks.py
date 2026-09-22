"""Deterministic hard gates for recommendation responses.

These checks only inspect the final user-visible result. Runtime guards are allowed
to repair or suppress unsafe candidates; a guard activation is therefore telemetry,
not a release failure. A hard failure means an invalid result still escaped.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.domain.enums import Concern


HANJA_PATTERN = re.compile(r"[\u4e00-\u9fff]")
BANNED_CONSUMER_TERMS = ("심부", "피장벽", "피분비", "지분")


@dataclass(frozen=True)
class HardFailure:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {
        key: getattr(value, key)
        for key in (
            "product_id", "product_name", "brand", "category", "matched_ingredients",
            "name", "kor_name",
        )
        if hasattr(value, key)
    }


def _product_bullets(text: str) -> list[str]:
    in_product_section = False
    bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = line.strip("# ").replace("*", "").strip()
        if heading == "추천 제품":
            in_product_section = True
            continue
        if in_product_section and line.startswith("-"):
            bullets.append(line)
    return bullets


def _check_product_grounding(
    text: str,
    ingredients: Sequence[Any],
    products: Sequence[Any],
) -> list[HardFailure]:
    failures: list[HardFailure] = []
    product_rows = [_as_dict(product) for product in products]
    ingredient_aliases: list[tuple[str, tuple[str, ...]]] = []
    for ingredient in ingredients:
        row = _as_dict(ingredient)
        canonical = str(row.get("name") or "").strip()
        if not canonical:
            continue
        aliases = tuple(
            str(alias).strip().casefold()
            for alias in (canonical, row.get("kor_name"))
            if alias and len(str(alias).strip()) >= 2
        )
        ingredient_aliases.append((canonical, aliases))

    for bullet in _product_bullets(text):
        matched = [
            product for product in product_rows
            if product.get("product_name") and str(product["product_name"]) in bullet
        ]
        if not matched:
            failures.append(HardFailure(
                "UNKNOWN_PRODUCT",
                f"provided products에 없는 추천 항목: {bullet[:160]}",
            ))
            continue

        allowed = {
            str(name).casefold()
            for product in matched
            for name in (product.get("matched_ingredients") or [])
        }
        # Product names may legitimately contain an ingredient-like token
        # (e.g. "나이아신아마이드 10 앰플"). It is a name, not an attributed
        # ingredient claim, so inspect only the explanatory remainder.
        claim_text = bullet
        for product in matched:
            claim_text = claim_text.replace(str(product.get("product_name") or ""), "")
        folded = claim_text.casefold()
        invalid = sorted({
            canonical
            for canonical, aliases in ingredient_aliases
            if any(alias in folded for alias in aliases) and canonical.casefold() not in allowed
        })
        if invalid:
            names = ", ".join(str(product.get("product_name")) for product in matched)
            failures.append(HardFailure(
                "PRODUCT_INGREDIENT_MISMATCH",
                f"{names}: 제품 매칭 근거에 없는 성분 {', '.join(invalid)}",
            ))
    return failures


def _check_target_mismatch(case: Mapping[str, Any], products: Sequence[Any]) -> list[HardFailure]:
    concern_values = case.get("concerns") or []
    concerns: list[Concern] = []
    for value in concern_values:
        try:
            concerns.append(value if isinstance(value, Concern) else Concern(str(value)))
        except ValueError:
            continue
    if not concerns or not products:
        return []

    # Keep the release check aligned with the exact same label data and fallback
    # heuristic used by the serving path instead of maintaining a second taxonomy.
    from app.services.recommend_service import filter_by_target_concerns

    product_rows = [_as_dict(product) for product in products]
    kept_ids = {
        str(product.get("product_id"))
        for product in filter_by_target_concerns(product_rows, concerns)
    }
    rejected = [
        str(product.get("product_name") or product.get("product_id"))
        for product in product_rows
        if str(product.get("product_id")) not in kept_ids
    ]
    if not rejected:
        return []
    return [HardFailure(
        "TARGET_MISMATCH",
        f"요청 고민과 제품 타겟 그룹 불일치: {', '.join(rejected)}",
    )]


def check_response(case: Mapping[str, Any], response: Any) -> list[HardFailure]:
    """Return every deterministic failure remaining in a final response."""
    text = str(_get(response, "response_text", "") or "")
    ingredients = list(_get(response, "ingredients", []) or [])
    products = list(_get(response, "products", []) or [])
    failures: list[HardFailure] = []

    if not text.strip():
        failures.append(HardFailure("EMPTY_RESPONSE", "response_text가 비어 있음"))

    hanja = sorted(set(HANJA_PATTERN.findall(text)))
    if hanja:
        failures.append(HardFailure("HANJA_LEAK", f"한자 노출: {''.join(hanja)}"))

    constraints = [str(value) for value in (case.get("constraints") or [])]
    if constraints and products:
        failures.append(HardFailure(
            "UNVERIFIED_CONSTRAINT",
            f"제품 속성 근거 없이 제약 제품 노출: {', '.join(constraints)}",
        ))

    for term in BANNED_CONSUMER_TERMS:
        if term in text:
            failures.append(HardFailure("BANNED_TERM", f"소비자 금칙 표현: {term}"))

    for product in products:
        row = _as_dict(product)
        brand = str(row.get("brand") or "").strip()
        product_name = str(row.get("product_name") or "").strip()
        raw = f"{brand} {product_name}".strip()
        if brand and product_name.casefold().startswith(brand.casefold()) and raw in text:
            failures.append(HardFailure(
                "BRAND_DUPLICATION",
                f"브랜드 중복 표기: {raw}",
            ))

    failures.extend(_check_product_grounding(text, ingredients, products))
    failures.extend(_check_target_mismatch(case, products))
    return failures


def summarize_hard_failures(rows: Sequence[Mapping[str, Any]], denominator: int) -> dict[str, Any]:
    """Aggregate case-level ``hard_failures`` into stable report metrics."""
    counts: Counter[str] = Counter()
    failed_cases = 0
    for row in rows:
        failures = row.get("hard_failures") or []
        if failures:
            failed_cases += 1
        for failure in failures:
            code = failure.get("code") if isinstance(failure, Mapping) else str(failure)
            counts[str(code)] += 1
    return {
        "hard_failure_cases": failed_cases,
        "hard_failure_count": sum(counts.values()),
        "hard_failure_rate": round(failed_cases / denominator, 4) if denominator else 0.0,
        "hard_failure_counts": dict(sorted(counts.items())),
    }
