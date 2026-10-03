"""제품별 '고민별 근거 성분 개수 + 핵심 성분' 요약.

- 개수는 논문(pubmed_evidence)·참고 도서(reference_book) 근거가 있는 성분만 센다. CosIng 기능 표기는 거의 모든
  성분에 붙어 숫자를 부풀리므로 제외한다. 고민 → 효능은 CONCERN_EFFECT_MAP을 따른다.
- 개수가 COUNT_MIN(3) 이상이면 숫자로, 그보다 적으면 성분 이름으로 말한다.
- 핵심 성분은 고민마다 최대 2개를 고르고 전체 KEY_MIN(2)~KEY_MAX(5)개로 맞춘다.
  우선순위: 그 고민의 식약처 고시 기능성 원료 → 논문 근거(점수순) → 참고 도서 근거.
  작용 근거가 없는 트러블 개선(BLEMISH_CARE)과 의약 표현에서 온 근거만 있는 성분은 개수에는 넣되 핵심 성분으로 고르지 않는다.
- 그래프에 전성분 순서가 없어 함량은 반영하지 못한다. 그래서 "효과가 있다"가 아니라 "관련 근거가 있는 성분"으로 말한다.
- 향료 rationale 제외 성분은 근거 개수와 핵심 성분 양쪽에서 제외한다. 제품 전성분 사실 표시는 별개다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.domain.enums import Concern, Effect
from app.services.taxonomy_normalization_service import CONCERN_EFFECT_MAP
from app.services.fragrance_policy import eligible_rationale

COUNT_MIN = 3
KEY_PER_CONCERN = 2
KEY_MIN, KEY_MAX = 2, 5
EVIDENCE_TYPES = ("pubmed_evidence", "reference_book")

CONCERN_LABEL_KO: dict[Concern, str] = {
    Concern.ACNE: "트러블", Concern.COMEDONES: "블랙헤드·화이트헤드", Concern.PORE_CONGESTION: "모공 막힘",
    Concern.ENLARGED_PORES: "모공", Concern.OILY_SKIN: "피지", Concern.SENSITIVE_SKIN: "민감",
    Concern.REDNESS: "붉은기", Concern.IRRITATED_SKIN: "자극", Concern.ATOPIC_PRONE: "아토피 피부",
    Concern.ROSACEA_PRONE: "홍조", Concern.DRY_SKIN: "건조", Concern.DEHYDRATED_SKIN: "속건조",
    Concern.FLAKY_SKIN: "각질", Concern.ROUGH_TEXTURE: "피부결", Concern.BARRIER_DAMAGE: "피부 장벽",
    Concern.HYPERPIGMENTATION: "미백", Concern.DULLNESS: "칙칙한 피부 톤", Concern.UNEVEN_SKIN_TONE: "피부 톤",
    Concern.BLEMISHES: "잡티", Concern.POST_ACNE_MARKS: "트러블 자국", Concern.DARK_CIRCLES: "다크서클",
    Concern.SUNBURN: "자외선", Concern.AGING_SIGNS: "노화", Concern.WRINKLES: "주름",
    Concern.LOSS_OF_ELASTICITY: "탄력", Concern.SAGGING_SKIN: "처짐",
}
# 고민 → 식약처 기능성 고시 기능(mfds_functional_ingredients.json의 function)
_FUNCTION_BY_CONCERN: dict[Concern, str] = {
    **{c: "whitening" for c in (Concern.HYPERPIGMENTATION, Concern.DULLNESS, Concern.UNEVEN_SKIN_TONE,
                                Concern.BLEMISHES, Concern.POST_ACNE_MARKS, Concern.DARK_CIRCLES)},
    **{c: "anti_wrinkle" for c in (Concern.AGING_SIGNS, Concern.WRINKLES, Concern.LOSS_OF_ELASTICITY,
                                   Concern.SAGGING_SKIN)},
    Concern.ACNE: "acne", Concern.SUNBURN: "uv_protection",
}
_FUNCTION_LABEL = {"whitening": "미백", "anti_wrinkle": "주름 개선", "acne": "여드름성 피부 완화",
                   "uv_protection": "자외선 차단"}
_PATH = Path(__file__).resolve().parent.parent / "data" / "mfds_functional_ingredients.json"


@lru_cache(maxsize=1)
def functional_ingredients() -> dict[str, list[str]]:
    return json.loads(_PATH.read_text(encoding="utf-8"))["ingredients"]


def summary_effects(concerns: list[Concern]) -> list[str]:
    return sorted({effect.value for concern in concerns for effect in CONCERN_EFFECT_MAP.get(concern, [])})


def _has_batchim(word: str) -> bool:
    last = word.strip()[-1:] if word.strip() else ""
    if "가" <= last <= "힣":
        return (ord(last) - 0xAC00) % 28 != 0
    return last.isdigit() and last in "013678"


def _subject(word: str) -> str:
    return f"{word}{'이' if _has_batchim(word) else '가'}"


def _display(row: dict[str, Any]) -> str:
    return row.get("kor_name") or row["inci_name"]


def _evidence_text(row: dict[str, Any]) -> str:
    if row["evidence_type"] == "pubmed_evidence":
        n = int(row.get("paper_count") or 0)
        return f"논문 근거 {n}건" if n else "논문 근거"
    return "참고 도서 근거"


def build_summary(concerns: list[Concern], rows: list[dict[str, Any]],
                  functional: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    """한 제품의 근거 행(성분×효능)으로 요약을 만든다. 근거 성분이 하나도 없으면 None."""
    functional = functional_ingredients() if functional is None else functional
    rows = [r for r in rows if r.get("evidence_type") in EVIDENCE_TYPES
            and eligible_rationale(r.get("inci_name"))]
    per_concern = []
    for concern in dict.fromkeys(concerns):
        effects = {e.value for e in CONCERN_EFFECT_MAP.get(concern, [])}
        by_ing: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["effect_code"] in effects:
                by_ing.setdefault(row["inci_name"], []).append(row)
        if by_ing:
            per_concern.append((concern, by_ing))
    if not per_concern:
        return None

    def candidates(concern: Concern, by_ing: dict[str, list[dict[str, Any]]]) -> list[tuple]:
        func = _FUNCTION_BY_CONCERN.get(concern)
        ranked = []
        for inci, ev in by_ing.items():
            usable = [r for r in ev if r["effect_code"] != Effect.BLEMISH_CARE.value and not r.get("medical_wording")]
            if not usable:
                continue
            best = max(usable, key=lambda r: (r["evidence_type"] == "pubmed_evidence", r.get("graph_score") or 0))
            is_func = bool(func) and func in functional.get(inci, [])
            ranked.append(((not is_func, best["evidence_type"] != "pubmed_evidence", -(best.get("graph_score") or 0),
                            _display(best)), inci, best, is_func, func))
        return sorted(ranked)

    pools = [candidates(c, ing) for c, ing in per_concern]
    chosen: dict[str, dict[str, Any]] = {}

    def take(pool: list[tuple], limit: int) -> None:
        taken = 0
        for _, inci, best, is_func, func in pool:
            if len(chosen) >= KEY_MAX or taken >= limit:
                return
            if inci in chosen:
                continue
            chosen[inci] = {
                "inci_name": inci, "name": _display(best), "evidence": _evidence_text(best),
                "mfds_functional": _FUNCTION_LABEL[func] if is_func else None,
                "effect_code": best["effect_code"],
            }
            taken += 1

    for pool in pools:
        take(pool, KEY_PER_CONCERN)
    if len(chosen) < KEY_MIN:
        for pool in pools:
            take(pool, KEY_MIN - len(chosen))

    concern_items = []
    for concern, by_ing in per_concern:
        names = sorted({_display(ev[0]) for ev in by_ing.values()})
        concern_items.append({"concern": concern.value, "label": CONCERN_LABEL_KO.get(concern, concern.value),
                              "count": len(by_ing), "names": names})
    return {"concerns": concern_items, "key_ingredients": list(chosen.values()),
            "sentence": render_sentence(concern_items)}


def render_sentence(items: list[dict[str, Any]]) -> str:
    """'미백 고민과 관련된 근거가 있는 성분 6가지, 주름 고민 관련 성분 15가지가 들어 있어요.' 형식."""
    counted = [i for i in items if i["count"] >= COUNT_MIN]
    named = [i for i in items if 0 < i["count"] < COUNT_MIN]
    sentences = []
    if counted:
        parts = [f"{i['label']} 고민{'과 관련된 근거가 있는' if n == 0 else ' 관련'} 성분 {i['count']}가지"
                 for n, i in enumerate(counted)]
        sentences.append(", ".join(parts) + "가 들어 있어요.")
    for i in named:
        sentences.append(f"{i['label']} 고민 관련 근거가 있는 성분으로는 {_subject('·'.join(i['names']))} 들어 있어요.")
    return " ".join(sentences)


def render_concern_summary(summary: dict[str, Any] | None) -> str:
    """생성 입력용 두 줄. 요약 문장은 그대로 쓰고, 핵심 성분만 설명하도록 표시한다."""
    if not summary or not summary.get("sentence"):
        return ""
    keys = []
    for item in summary["key_ingredients"]:
        notes = [f"식약처 고시 {item['mfds_functional']} 원료"] if item["mfds_functional"] else []
        keys.append(f"{item['name']} ({', '.join(notes + [item['evidence']])})")
    lines = [f"  · 고민별 근거 성분(문장 그대로 사용): {summary['sentence']}"]
    if keys:
        lines.append(f"  · 핵심 성분(이 성분만 설명): {'; '.join(keys)}")
    return "\n".join(lines)
