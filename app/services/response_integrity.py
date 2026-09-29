"""Conservative, deterministic checks for visibly corrupted recommendation text."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence
from app.services.ingredient_explanations import product_explanations


# Even one duplicated content word can be visibly broken prose ("세포 세포을").
# Allow common intentional reduplications while catching a Korean case particle
# appended to the second occurrence.
_REPEATED_KOREAN_WORD = re.compile(
    r"(?<![가-힣])([가-힣]{2,})(?:\s+\1)+"
    r"(?=$|[^가-힣]|[은는이가을를의도만와과에로](?=$|[^가-힣]))"
)
_ALLOWED_REDUPLICATIONS = {"매일", "조금", "서로", "자꾸"}
_LONG_UPPERCASE_TOKEN = re.compile(r"(?<![A-Za-z])[A-Z]{6,}(?![A-Za-z])")
_PARENTHETICAL = re.compile(r"\([^()]*\)")


def _field(row: Any, name: str) -> str:
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return str(value or "").strip()


def find_response_integrity_issues(
    text: str,
    ingredients: Sequence[Any],
    products: Sequence[Any],
) -> list[tuple[str, str]]:
    """Find high-confidence degeneration without rejecting known names or INCI labels.

    A long uppercase word in explanatory prose is suspicious, but the same word
    in a supplied ingredient/brand/product name or parenthetical label is allowed.
    This intentionally favors precision over catching every English intrusion.
    """
    issues: list[tuple[str, str]] = []
    repeated = next(
        (match for match in _REPEATED_KOREAN_WORD.finditer(text)
         if match.group(1) not in _ALLOWED_REDUPLICATIONS),
        None,
    )
    if repeated:
        issues.append(("DEGENERATE_REPETITION", f"연속 단어 반복: {repeated.group(0)[:80]}"))

    prose = _PARENTHETICAL.sub(" ", text)
    known_names = [
        _field(row, field)
        for rows, fields in (
            (ingredients, ("name", "kor_name")),
            (products, ("product_name", "brand")),
        )
        for row in rows
        for field in fields
    ]
    known_names.extend(name for product in products for card in product_explanations(product)
                       for name in (card.name, card.kor_name))
    for name in sorted((name for name in known_names if name), key=len, reverse=True):
        prose = re.sub(re.escape(name), " ", prose, flags=re.IGNORECASE)
    stray = _LONG_UPPERCASE_TOKEN.search(prose)
    if stray:
        issues.append(("STRAY_ENGLISH_TOKEN", f"설명에 섞인 영문 토큰: {stray.group(0)}"))
    return issues
