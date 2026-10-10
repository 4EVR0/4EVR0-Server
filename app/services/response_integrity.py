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
# "한글명 (INCI)" 표기의 INCI 부분.
_INCI_LABEL = re.compile(r"[가-힣][^()\n]{0,40}?\(\s*([A-Z0-9][A-Z0-9 ,.'/\-]{3,}?)\s*\)")
# INCI 바로 앞의 한글 이름(강조 표시 허용). "1,2-헥산다이올"처럼 숫자로 시작하면 한글 부분만 잡힌다.
_KOREAN_BEFORE_INCI = re.compile(r"([가-힣][가-힣0-9/\-]*)\**\s*\**\s*\(\s*([A-Z0-9][A-Z0-9 ,.'/\-]{3,}?)\s*\)")
# *성분* / **성분** 강조 표기.
_EMPHASIS = re.compile(r"\*{1,2}([^*\n]{2,40}?)\*{1,2}")
# 그래프에 없는 이름이라도 성분처럼 보이는 어미(지어낸 성분명 판별용).
_INGREDIENT_SUFFIX = re.compile(r"(?:애씨드|추출물|오일|아마이드|펩타이드|세라마이드|글루칸|에이트|레이트|솔|론|놀|올|틴)$")


def _field(row: Any, name: str) -> str:
    value = row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)
    return str(value or "").strip()


def _key(name: str) -> str:
    return re.sub(r"[\s\-]", "", name).casefold()


def _unknown_ingredient(
    text: str,
    ingredients: Sequence[Any],
    products: Sequence[Any],
    inventory: Mapping[str, Sequence[Mapping[str, Any]]],
    vocabulary: frozenset[str],
) -> str | None:
    """보여 주는 제품·성분에서 확인되지 않은 성분명을 찾는다.

    - "한글명 (INCI)"의 INCI가 확인된 성분이 아니면 걸린다.
    - INCI는 맞아도 앞의 한글명이 그 성분의 한글명과 다르면 걸린다(예: 마데카씨드 (MADECASSOSIDE)).
    - 강조한 한글 이름이 그래프 성분인데 보여 주는 제품에 없으면 걸린다.
    - 그래프에도 없는 이름은 성분 어미(애씨드·추출물·솔·론 등)로 끝날 때만 걸린다(지어낸 성분명).
    제품명·브랜드 일부, 확인된 성분명 일부(예: 세라마이드엔피 → 세라마이드)는 허용한다.
    """
    allowed = {_key(_field(row, field)) for row in ingredients for field in ("name", "kor_name")}
    allowed |= {_key(name) for product in products for card in product_explanations(product)
                for name in (card.name, card.kor_name) if name}
    allowed |= {_key(str(row.get(field) or "")) for rows in inventory.values() for row in rows
                for field in ("name", "kor_name")}
    allowed.discard("")
    korean_by_inci: dict[str, str] = {}
    pairs = [(_field(row, "name"), _field(row, "kor_name")) for row in ingredients]
    pairs += [(card.name or "", card.kor_name or "") for product in products for card in product_explanations(product)]
    pairs += [(str(row.get("name") or ""), str(row.get("kor_name") or "")) for rows in inventory.values() for row in rows]
    for inci, kor in pairs:
        if inci and kor:
            korean_by_inci.setdefault(_key(inci), _key(kor))
    labels = [_key(_field(product, field)) for product in products for field in ("product_name", "brand")]
    labels = [label for label in labels if label]

    def known(name: str) -> bool:
        key = _key(name)
        return (key in allowed or any(key in label for label in labels)
                or (len(key) >= 3 and any(key in item for item in allowed)))

    for match in _INCI_LABEL.finditer(text):
        if not known(match.group(1)):
            return match.group(0).strip()
    for match in _KOREAN_BEFORE_INCI.finditer(text):
        written, expected = _key(match.group(1)), korean_by_inci.get(_key(match.group(2)))
        if expected and written not in expected and expected not in written:
            return match.group(0).strip()
    for match in _EMPHASIS.finditer(text):
        name = _PARENTHETICAL.sub("", match.group(1)).strip()
        if not re.fullmatch(r"[가-힣][가-힣0-9\s\-]*", name) or known(name):
            continue
        if _key(name) in vocabulary or _INGREDIENT_SUFFIX.search(name):
            return name
    return None


_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+|\n")
_ALL_PRODUCTS = re.compile(r"(?:모든|각|전)\s*제품|제품(?:\s*(?:모두|전부|마다))")
# "우레아나 하이알루로닉애씨드" — 제품마다 둘 중 하나만 있으면 된다.
_DISJUNCTION = re.compile(r"(?:나|또는|혹은)\s")
_NEGATION = re.compile(r"없|빠져|제외|않")


def _misattributed_ingredient(
    text: str,
    ingredients: Sequence[Any],
    products: Sequence[Any],
    inventory: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str | None:
    """제품과 성분을 잘못 연결한 문장을 찾는다.

    - 강조(*·**)한 제품명과 성분이 한 문장에 나오면, 그 성분은 언급한 제품 중 하나의 전성분에 있어야 한다.
    - "모든 제품·각 제품·제품마다"라고 하면 그 성분은 보여 주는 제품 전부의 전성분에 있어야 한다.
      "A나 B"처럼 고르는 문장이면 제품마다 둘 중 하나만 있으면 된다. "여러 제품에 공통"은 전부가 아니다.
    부정 문장("~에는 없어요")은 보지 않는다. 전성분을 모르는 제품이 섞이면 판단하지 않는다.
    """
    contents = {pid: {_key(str(row.get(field) or "")) for row in rows for field in ("name", "kor_name")} - {""}
                for pid, rows in inventory.items()}
    shown = [product for product in products if _field(product, "product_id") in contents]
    if not shown:
        return None
    # 성분 이름(한글명 3자 이상·INCI 4자 이상) → INCI 키. 긴 이름부터 찾는다(세라마이드엔피 > 세라마이드).
    names: dict[str, str] = {}
    rows = [row for rows in inventory.values() for row in rows]
    for name, kor in [(str(r.get("name") or ""), str(r.get("kor_name") or "")) for r in rows] + [
            (_field(r, "name"), _field(r, "kor_name")) for r in ingredients]:
        if len(_key(kor)) >= 3:
            names.setdefault(_key(kor), _key(name))
        if len(_key(name)) >= 4:
            names.setdefault(_key(name), _key(name))
    ordered = sorted(names, key=len, reverse=True)
    product_keys = [(_key(_field(product, "product_name")), product) for product in shown]

    for sentence in _SENTENCE_END.split(text):
        if not sentence.strip() or _NEGATION.search(sentence):
            continue
        mentioned = []
        rest = sentence
        for match in _EMPHASIS.finditer(sentence):
            key = _key(_PARENTHETICAL.sub("", match.group(1)))
            hits = [product for pkey, product in product_keys if len(key) >= 4 and (key in pkey or pkey in key)]
            if hits:
                mentioned.extend(hits)
                rest = rest.replace(match.group(0), " ")
        everyone = bool(_ALL_PRODUCTS.search(rest))
        if not mentioned and not everyone:
            continue
        folded = _key(rest)
        said = []
        for name in ordered:
            if name in folded:
                said.append(names[name])
                folded = folded.replace(name, " ")
        said = list(dict.fromkeys(said))
        if everyone and said and _DISJUNCTION.search(rest):
            if any(not any(inci in contents[_field(product, "product_id")] for inci in said) for product in shown):
                return sentence.strip()
            continue
        for inci in said:
            holders = [product for product in shown if inci in contents[_field(product, "product_id")]]
            if everyone and len(holders) < len(shown):
                return sentence.strip()
            if mentioned and not any(product in holders for product in mentioned):
                return sentence.strip()
    return None


def find_response_integrity_issues(
    text: str,
    ingredients: Sequence[Any],
    products: Sequence[Any],
    inventory: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    vocabulary: frozenset[str] = frozenset(),
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
    # 제품 전성분을 넘긴 경우에만 본다(전성분 없이는 '확인되지 않음'을 판단할 수 없다).
    if inventory:
        unknown = _unknown_ingredient(text, ingredients, products, inventory, vocabulary)
        if unknown:
            issues.append(("UNKNOWN_INGREDIENT", f"확인되지 않은 성분명: {unknown[:80]}"))
        misattributed = _misattributed_ingredient(text, ingredients, products, inventory)
        if misattributed:
            issues.append(("MISATTRIBUTED_INGREDIENT", f"제품에 없는 성분 연결: {misattributed[:80]}"))
    return issues
