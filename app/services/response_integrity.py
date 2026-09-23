"""Conservative, deterministic checks for visibly corrupted recommendation text."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


# Four adjacent repetitions are unlikely to be intentional consumer-facing prose.
_REPEATED_KOREAN_WORD = re.compile(r"(?<![가-힣])([가-힣]{2,})(?:\s+\1){3,}(?![가-힣])")
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
    repeated = _REPEATED_KOREAN_WORD.search(text)
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
    for name in sorted((name for name in known_names if name), key=len, reverse=True):
        prose = re.sub(re.escape(name), " ", prose, flags=re.IGNORECASE)
    stray = _LONG_UPPERCASE_TOKEN.search(prose)
    if stray:
        issues.append(("STRAY_ENGLISH_TOKEN", f"설명에 섞인 영문 토큰: {stray.group(0)}"))
    return issues
