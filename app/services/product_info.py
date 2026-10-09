"""특정 제품 설명 요청("제품 A 어때?", #124).

제품 설명은 하드코딩하지 않는다. 사용자가 지목한 제품을 그래프에서 이름으로 찾고, 확인된 성분(CONTAINS)의
근거 효능을 효능별로 묶어 서버가 설명을 모두 만든다(소개 문장 포함, 생성 모델 문장은 쓰지 않음).

- 제품은 서버가 정한다. 생성 모델(프로필 추출)은 사용자가 쓴 제품 이름 문자열만 넘긴다.
- 후보가 여러 개면 되묻고, 없으면 없다고 답한다. 없는 제품을 지어내지 않는다.
- 그래프에는 함량·전성분 순서가 없다. "주성분", "많이 들었다"는 말하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services import sensitive_caution
from app.services.concern_summary import functional_ingredients
from app.services.fragrance_allergens import allergen_note, allergens_in
from app.services.fragrance_policy import eligible_rationale

# ── 질문 인식(규칙) ──────────────────────────────────────────────────────────
INFO_CUE = re.compile(
    r"어때|어떤가요|어떤\s*(?:제품|화장품|거|건)|무슨\s*(?:제품|화장품)|뭐\s*(?:야|예요|에요)|"
    r"설명해|소개해|알려\s*(?:줘|주세요|줄래)|성분\s*(?:이|은|좀)?\s*(?:뭐|어때|알려)|괜찮(?:아|은가|을까|나요)"
)
_PARTICLES = ("이에요", "예요", "이야", "으로", "에서", "이랑", "하고", "은", "는", "이", "가", "을", "를",
              "도", "요", "에", "의", "로", "랑", "야")
_STOPWORDS = frozenset({
    # 질문·지시 표현
    "어때", "어때요", "어떤가요", "어떤", "무슨", "뭐야", "뭐예요", "뭐에요", "설명", "설명해", "설명해줘", "설명해주세요",
    "소개", "소개해줘", "알려줘", "알려주세요", "알려줄래", "괜찮아", "괜찮나요", "괜찮을까", "괜찮은가요",
    "혹시", "좀", "정말", "진짜", "그냥", "한번", "써도", "쓰면", "사용", "사용해도", "추천", "추천해줘",
    "이거", "그거", "저거", "이것", "그것", "저것", "이", "그", "저", "제품", "화장품", "성분", "효능", "효과",
    "피부", "제", "내", "나", "저는", "나는", "써보려고", "사려고", "살까", "궁금해", "궁금해요",
    # 질문 어미(질문 표현을 지운 뒤 남는 조각)
    "이야", "이에요", "예요", "인가요", "인가", "일까", "인지", "입니까", "건가요", "건가", "거야",
})
# 이 말들만으로는 특정 제품을 가리키지 않는다(토큰으로는 쓰되 하나 이상은 이 밖의 말이어야 한다).
_GENERIC = frozenset({
    "토너", "스킨", "로션", "크림", "세럼", "앰플", "에센스", "클렌저", "클렌징", "폼", "선크림", "선블록",
    "마스크", "팩", "패드", "미스트", "오일", "밤", "젤", "올인원", "아이크림", "수분크림", "보습크림",
    "여드름", "모공", "블랙헤드", "피지", "지성", "건성", "복합성", "중성", "민감성", "건조", "속건조", "수분",
    "민감", "홍조", "트러블", "기미", "잡티", "미백", "주름", "탄력", "각질", "아토피", "진정", "보습",
})
TOO_BROAD = 21  # 이 수 이상 걸리면 특정 제품이 아니라고 본다(query 한도와 같음)
MAX_CHOICES = 5


def _strip_particle(word: str) -> str:
    for suffix in _PARTICLES:
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            return word[: -len(suffix)]
    return word


def mention_tokens(text: str) -> list[str]:
    """제품 이름 대조용 토큰(소문자, 기호 제거). 지시·질문 표현은 뺀다. 특정 제품을 가리키지 않으면 빈 목록."""
    cleaned = INFO_CUE.sub(" ", text)
    words = re.findall(r"[0-9A-Za-z가-힣]+", cleaned)
    tokens: list[str] = []
    for word in words:
        word = _strip_particle(word.casefold())
        if len(word) < 2 or word in _STOPWORDS or re.fullmatch(r"\d+(?:번|번째)?", word):
            continue
        if word not in tokens:
            tokens.append(word)
    if not any(t not in _GENERIC for t in tokens):
        return []
    return tokens


def has_brand(tokens: list[str], brands: frozenset[str]) -> bool:
    """토큰 중 하나가 브랜드 이름이거나 브랜드로 시작하는가(띄어쓰기 없이 붙여 쓴 경우)."""
    return any(t in brands or any(len(b) >= 2 and t.startswith(b) for b in brands) for t in tokens)


def looks_like_product_info(message: str) -> bool:
    return bool(INFO_CUE.search(message))


# ── 되묻기 후 선택 ───────────────────────────────────────────────────────────
_ORDINALS = {"첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5}


def choice_from_message(message: str, choices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """되묻기 목록에서 번호("2번", "두 번째") 또는 이름으로 고른 제품. 못 고르면 None."""
    text = message.strip()
    number = re.match(r"^\s*(\d+)\s*(?:번|번째)?", text)
    index = int(number.group(1)) if number else None
    if index is None:
        ordinal = re.match(r"^\s*(첫|두|세|네|다섯)\s*번째", text)
        index = _ORDINALS[ordinal.group(1)] if ordinal else None
    if index is not None:
        return choices[index - 1] if 1 <= index <= len(choices) else None
    tokens = mention_tokens(text) or [t for t in (_strip_particle(w.casefold()) for w in re.findall(r"[0-9A-Za-z가-힣]+", text))
                                     if len(t) >= 2 and t not in _STOPWORDS]
    if not tokens:
        return None
    hits = [c for c in choices
            if all(t in re.sub(r"[\s-]", "", f"{c.get('brand') or ''}{c.get('product_name') or ''}".casefold())
                   for t in tokens)]
    return hits[0] if len(hits) == 1 else None


# ── 설명 구성 ────────────────────────────────────────────────────────────────
EFFECT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("보습", ("HYDRATING", "MOISTURE_RETENTION")),
    ("피부 장벽", ("BARRIER_REPAIR",)),
    ("진정", ("SOOTHING", "ANTI_INFLAMMATORY")),
    ("미백·피부 톤", ("DEPIGMENTING", "BRIGHTENING")),
    ("주름·탄력", ("ANTI_AGING",)),
    ("피지·각질·모공", ("SEBUM_REGULATION", "KERATOLYTIC", "COMEDOLYTIC")),
    ("항균", ("ANTIMICROBIAL",)),
    ("항산화", ("ANTIOXIDANT",)),
    ("자외선 차단", ("PHOTOPROTECTIVE",)),
    ("피부 회복", ("WOUND_HEALING",)),
)
_GROUP_BY_EFFECT = {code: label for label, codes in EFFECT_GROUPS for code in codes}
_FUNCTION_GROUP = {"whitening": ("미백·피부 톤", "미백"), "anti_wrinkle": ("주름·탄력", "주름 개선"),
                   "acne": ("피지·각질·모공", "여드름성 피부 완화"), "uv_protection": ("자외선 차단", "자외선 차단")}
MAX_GROUPS = 6
MAX_PER_GROUP = 4
MAX_GROUPS_PER_INGREDIENT = 2  # 같은 성분이 여러 묶음에 반복되지 않게, 근거가 가장 강한 묶음 2개까지
MIN_PAPERS = 2  # 논문 1편뿐인 효능은 참고 도서·식약처 고시 근거가 없으면 넣지 않는다
LIMITATION_NOTE = ("참고: 확인된 성분 기준이에요. 그래프에는 함량과 전성분 순서가 없어 어떤 성분이 많이 들었는지는 알 수 없고, "
                   "\"효과가 있다\"가 아니라 관련 근거가 있는 성분이 들어 있다는 뜻이에요.")


@dataclass
class IngredientEvidence:
    inci: str
    name: str
    papers: int = 0  # 논문 근거 편수(효능 묶음 안 최댓값)
    book: bool = False
    functional: str | None = None  # 식약처 고시 기능 문구
    effects: set[str] = field(default_factory=set)

    def label(self) -> str:
        parts = [f"식약처 고시 {self.functional} 원료"] if self.functional else []
        if self.papers:
            parts.append(f"논문 근거 {self.papers}건")
        elif self.book:
            parts.append("참고 도서 근거")
        return f"{self.name}({', '.join(parts)})" if parts else self.name

    def strength(self) -> tuple:
        return (self.functional is not None, self.papers, self.book)


def effect_name_en(code: str) -> str:
    """효능 코드 → 효능 검사(ingredient_claim_guard)가 읽는 영문 이름."""
    return code.replace("_", " ").capitalize()


def group_facts(rows: list[dict[str, Any]]) -> list[tuple[str, list[IngredientEvidence]]]:
    """성분별 근거 행 → [(효능 묶음, 성분 목록)]. 근거가 없거나 향료 추천 근거 제외 성분은 넣지 않는다."""
    functional = functional_ingredients()
    groups: dict[str, dict[str, IngredientEvidence]] = {}
    for row in rows:
        inci = str(row.get("inci_name") or "")
        if not inci or not eligible_rationale(inci):
            continue
        name = row.get("kor_name") or inci
        per_group: dict[str, IngredientEvidence] = {}

        def item(label: str) -> IngredientEvidence:
            return per_group.setdefault(label, IngredientEvidence(inci=inci, name=name))

        for ev in row.get("evidence") or []:
            if not ev:
                continue
            for code in str(ev.get("effects") or "").split("|"):
                label = _GROUP_BY_EFFECT.get(code)
                if label:
                    it = item(label)
                    it.papers = max(it.papers, int(ev.get("papers") or 0))
                    it.effects.add(code)
        for af in row.get("affects") or []:
            if not af or af.get("medical"):
                continue
            label = _GROUP_BY_EFFECT.get(str(af.get("effect")))
            if not label:
                continue
            it = item(label)
            it.effects.add(str(af["effect"]))
            if af.get("type") == "pubmed_evidence":
                it.papers = max(it.papers, int(af.get("papers") or 0))
            else:
                it.book = True
        for function in functional.get(inci, []):
            mapped = _FUNCTION_GROUP.get(function)
            if mapped:
                item(mapped[0]).functional = mapped[1]
        kept = [(label, it) for label, it in per_group.items()
                if it.functional or it.book or it.papers >= MIN_PAPERS]
        kept.sort(key=lambda kv: kv[1].strength(), reverse=True)
        for label, it in kept[:MAX_GROUPS_PER_INGREDIENT]:
            groups.setdefault(label, {})[inci] = it
    order = {label: idx for idx, (label, _) in enumerate(EFFECT_GROUPS)}
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), order[kv[0]]))[:MAX_GROUPS]
    return [(label, sorted(items.values(), key=lambda it: it.strength(), reverse=True)[:MAX_PER_GROUP])
            for label, items in ranked]


def _topic(word: str) -> str:
    """받침에 맞춘 주제 조사. 끝의 괄호·기호는 건너뛰고 마지막 글자로 정한다("EX(크림)" → 은)."""
    letters = re.sub(r"[^0-9A-Za-z가-힣]+$", "", word.strip())
    last = letters[-1:] if letters else ""
    batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return f"{word}{'은' if batchim else '는'}"


def default_intro(display_name: str, category: str | None, total: int, with_evidence: int) -> str:
    kind = f"({category})" if category else ""
    if not with_evidence:
        return (f"{_topic(display_name + kind)} 확인된 성분 {total}개 가운데 효능 근거가 있는 성분을 찾지 못했어요. "
                "성분 정보가 부족할 수 있으니 제품 상세 정보의 전성분을 함께 확인해 주세요.")
    return f"{_topic(display_name + kind)} 확인된 성분 {total}개 가운데 {with_evidence}개가 효능 근거가 있는 성분이에요."


def render_sections(groups: list[tuple[str, list[IngredientEvidence]]], rows: list[dict[str, Any]]) -> str:
    """효능별 성분, 배합한도, 한계, 민감 주의 안내."""
    lines: list[str] = []
    if groups:
        lines.append("효능별 성분")
        lines += [f"- {label}: {', '.join(it.label() for it in items)}" for label, items in groups]
    shown = {it.inci for _, items in groups for it in items}
    limited = [f"{r.get('kor_name') or r['inci_name']}({r['kr_limit_note']})" for r in rows
               if r["inci_name"] in shown and r.get("kr_reg_status") == "restricted" and r.get("kr_limit_note")]
    if limited:
        lines.append("")
        lines.append("국내 배합한도가 있는 성분: " + "; ".join(limited))
    lines += ["", LIMITATION_NOTE]
    caution = sensitive_caution.notes_text(
        (r.get("kor_name") or r["inci_name"], sensitive_caution.CAUTION_NOTE) for r in rows
        if r["inci_name"] in shown and r.get("sensitive_caution") in ("exclude", "caution"))
    if caution:
        lines.append(caution)
    return "\n".join(lines)


def product_allergen_note(display_name: str, rows: list[dict[str, Any]]) -> str | None:
    return allergen_note([(display_name, allergens_in({"name": r["inci_name"]} for r in rows))])


def choice_text(candidates: list[dict[str, Any]], display) -> str:
    lines = ["말씀하신 이름으로 여러 제품이 있어요. 어떤 제품인지 번호나 이름으로 알려 주세요."]
    lines += [f"{idx}. {display(c.get('brand'), c.get('product_name'))} ({c.get('category') or '분류 없음'})"
              for idx, c in enumerate(candidates[:MAX_CHOICES], 1)]
    return "\n".join(lines)


NOT_FOUND_TEXT = ("말씀하신 이름의 제품을 찾지 못했어요. 브랜드와 제품명을 조금 다르게(띄어쓰기·영문 표기 등) 적어 "
                  "다시 물어봐 주세요.")
