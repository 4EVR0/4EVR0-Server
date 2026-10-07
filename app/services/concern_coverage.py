"""복합 고민 검색에서 고민 → 효능 → 성분 연결을 유지한다(이슈 #113).

- 성분 후보를 고민별로 따로 조회한 뒤, 후보 목록과 제품 조회 풀을 고민마다 번갈아 채운다.
  한 고민의 성분 점수가 높다고 다른 고민의 성분이 풀에서 사라지지 않게 하려는 것이다.
  후보·풀의 크기는 기존 설정 그대로이며 늘리지 않는다.
- 각 성분에는 그 성분이 후보로 나온 고민을 모두 붙인다(한 성분이 여러 고민의 근거일 수 있음).
- 제품 커버리지 = 제품이 실제로 함유한(CONTAINS) 풀 성분이 근거가 되는 고민의 집합.
  풀 밖의 성분은 보지 않으므로 "근거 없음"이 아니라 "이번 검색의 대표 성분으로는 확인 안 됨"이다.
- 고민이 하나면 기존 결과와 같다.
"""

from __future__ import annotations

from typing import Any, Sequence

from app.domain.enums import Concern
from app.services.concern_summary import CONCERN_LABEL_KO


def _rank(row: dict[str, Any]) -> tuple:
    # query_ingredients_by_effects와 같은 순서: 고민별 논문 근거 → 논문 근거 → 그 외 → 결과 효능(BLEMISH_CARE),
    # 점수 내림차순.
    if row.get("eligibility_tier") == "pubmed_review":
        ev_rank = -1
    else:
        ev_rank = 2 if row.get("claim") == "Blemish care" else (
            0 if row.get("eligibility_tier") == "pubmed_evidence" else 1)
    return ev_rank, -float(row.get("graph_score") or 0.0)


def _interleave(lists: Sequence[Sequence[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """각 목록의 순서를 지키며 번갈아 하나씩 꺼내 중복(name) 없이 limit개까지 모은다."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursors = [0] * len(lists)
    while len(out) < limit:
        progressed = False
        for idx, rows in enumerate(lists):
            while cursors[idx] < len(rows):
                row = rows[cursors[idx]]
                cursors[idx] += 1
                if row["name"] not in seen:
                    seen.add(row["name"])
                    out.append(row)
                    progressed = True
                    break
            if len(out) >= limit:
                break
        if not progressed:
            break
    return out


def merge_concern_candidates(
    rows_by_concern: dict[Concern, list[dict[str, Any]]], limit: int,
) -> list[dict[str, Any]]:
    """고민별 성분 후보를 하나의 후보 목록으로 합친다.

    같은 성분이 여러 고민에서 나오면 근거 등급·점수가 가장 높은 행을 대표로 쓰고,
    `concerns`에 해당 고민을 모두 남긴다. 고민이 하나면 입력 순서·내용을 그대로 반환한다.
    """
    concerns_by_name: dict[str, list[str]] = {}
    best: dict[str, dict[str, Any]] = {}
    for concern, rows in rows_by_concern.items():
        for row in rows:
            name = row["name"]
            concerns_by_name.setdefault(name, [])
            if concern.value not in concerns_by_name[name]:
                concerns_by_name[name].append(concern.value)
            if name not in best or _rank(row) < _rank(best[name]):
                best[name] = row
    ordered = [
        [{**best[row["name"]], "concerns": concerns_by_name[row["name"]]} for row in rows]
        for rows in rows_by_concern.values()
    ]
    return _interleave(ordered, limit)


def concern_ingredient_pool(candidates: list[dict[str, Any]], concerns: list[Concern],
                            size: int) -> list[dict[str, Any]]:
    """제품 조회에 넘길 성분 풀. 고민마다 자기 후보 순서대로 번갈아 채운다.

    반환 행: {"name", "weight", "concerns"} — weight는 기존과 같이 대표 근거의 graph_score.
    """
    codes = [c.value for c in dict.fromkeys(concerns)]
    if len(codes) <= 1:
        picked = candidates[:size]
    else:
        per_concern = [[r for r in candidates if code in (r.get("concerns") or [])] for code in codes]
        picked = _interleave(per_concern, size)
    return [
        {"name": r["name"], "weight": float(r.get("graph_score") or 1.0),
         "concerns": list(r.get("concerns") or codes)}
        for r in picked
    ]


def product_coverage(product: dict[str, Any], pool: list[dict[str, Any]]) -> list[str]:
    """제품이 실제로 함유한 풀 성분이 근거가 되는 고민 코드(풀의 고민 순서)."""
    matched = set(product.get("matched_ingredients") or [])
    covered: list[str] = []
    for row in pool:
        if row["name"] in matched:
            for code in row.get("concerns") or []:
                if code not in covered:
                    covered.append(code)
    order = {code: idx for idx, code in enumerate(_pool_concerns(pool))}
    return sorted(covered, key=lambda code: order.get(code, len(order)))


def _pool_concerns(pool: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for row in pool:
        for code in row.get("concerns") or []:
            if code not in codes:
                codes.append(code)
    return codes


def order_by_coverage(products: list[dict[str, Any]], concerns: list[Concern],
                      pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """복합 고민이면 요청 고민을 더 많이 충족하는 제품을 앞에 둔다.

    충족 고민 수가 같으면 기존 순서(관련도 버킷·목적 라벨·리뷰)를 유지한다(stable sort).
    모든 고민을 충족하는 제품이 하나도 없으면 고민별 제품이 번갈아 나오게 해
    한 고민 제품만 상위를 차지하지 않게 한다. 각 제품에 `concern_coverage`를 붙인다.
    """
    codes = [c.value for c in dict.fromkeys(concerns)]
    for product in products:
        product["concern_coverage"] = product_coverage(product, pool)
    if len(codes) <= 1:
        return products
    wanted = set(codes)
    ranked = sorted(products, key=lambda p: -len(wanted & set(p["concern_coverage"])))
    if any(wanted <= set(p["concern_coverage"]) for p in ranked):
        return ranked
    # 부분 충족만 있을 때: 고민별로 그 고민을 충족하는 제품을 번갈아 배치(고민 순서 = 요청 순서).
    buckets = [[p for p in ranked if code in p["concern_coverage"]] for code in codes]
    rest = [p for p in ranked if not wanted & set(p["concern_coverage"])]
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    cursors = [0] * len(buckets)
    while True:
        progressed = False
        for idx, bucket in enumerate(buckets):
            while cursors[idx] < len(bucket) and id(bucket[cursors[idx]]) in seen:
                cursors[idx] += 1
            if cursors[idx] < len(bucket):
                product = bucket[cursors[idx]]
                seen.add(id(product))
                out.append(product)
                progressed = True
        if not progressed:
            break
    return out + rest


def partial_coverage_note(products: list[tuple[str, list[str]]], concerns: list[Concern]) -> str | None:
    """복합 고민인데 모든 고민을 함께 충족하는 추천 제품이 없으면 부분 충족 안내를 만든다.

    products: [(표시 이름, concern_coverage)] — 사용자에게 보이는 최종 제품 순서.
    효능 우열이나 함량을 말하지 않고, 이번 검색의 대표 근거 성분이 어느 고민에 닿는지만 적는다.
    """
    codes = [c.value for c in dict.fromkeys(concerns)]
    if len(codes) <= 1 or not products:
        return None
    wanted = set(codes)
    if any(wanted <= set(coverage) for _, coverage in products):
        return None
    labels = [CONCERN_LABEL_KO.get(Concern(code), code) for code in codes]
    lines = [
        f"참고: 요청하신 {'·'.join(labels)} 고민을 모두 뒷받침하는 대표 근거 성분이 함께 들어 있는 제품은 "
        "이번 검색에서 찾지 못해, 고민별로 나눠 골랐어요."
    ]
    for name, coverage in products:
        covered = [CONCERN_LABEL_KO.get(Concern(code), code) for code in coverage if code in wanted]
        lines.append(f"- {name}: " + (f"{'·'.join(covered)} 고민 관련 성분 포함" if covered
                                      else "요청 고민의 대표 근거 성분은 확인되지 않음"))
    return "\n".join(lines)
