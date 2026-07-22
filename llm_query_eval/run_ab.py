"""A(프로덕션 파이프라인) vs B(LLM 생성 Cypher) 제품 추천 품질 비교.

사용자는 성분이 아니라 제품을 원하므로, A/B 둘 다 최종적으로 **제품**을 비교한다.

  - A: 프로덕션이 실제로 하는 것 그대로 재현 —
    query_ingredients_by_effects → apply_caution_filter → select_products
    (전부 app.services.recommend_service/app.clients.neo4j_client 함수를 직접
    호출. 텍스트 복사 안 함 — 복사본은 원본과 어긋날 수 있고, 실제로 이번 작업
    중 pg_experiment/queries.py와 eval/graphrag_ranking_eval.py의 복사본이
    프로덕션과 어긋나 있는 걸 발견했음)
  - B: LLM이 원문 메시지만 보고 생성한 Cypher(EXPLAIN 검증 통과분)로 제품까지 직접 조회

채점 기준 3가지 (product_category_eval.py 방식 + 독립 신호):
  - category_fit: 반환된 제품 카테고리가 concern에 적합한가 (recommend_service의
    _appropriate_categories 그대로 재사용 — 씻어내는 제품/부적합 제형 배제)
  - ingredient_grounded: 그 제품이 실제로 gold 성분(pubmed_evidence 근거)을 포함하는가
  - review_score: 올리브영 실제 구매자 리뷰 태그(review_stats. "보습/주름·미백/진정"
    3개 대분류)와 겹치는 concern이면 그 비율. 그래프 자체 데이터가 아니라 실제
    구매자가 남긴 독립적인 신호라, 그래프가 틀렸어도 걸러낼 수 있음 — 다만 3개
    대분류뿐이라 해당하는 concern에서만 계산됨(해당 없으면 None).

정답 성분 기준은 여전히 eval/gold_labels.py의 AFFECTS 엣지
evidence_type == 'pubmed_evidence'.

사용:
    python run_ab.py --limit 3      # 시나리오 3개만 (파이프라인 확인용)
    python run_ab.py                # 전체
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from neo4j import GraphDatabase

_APP_ROOT = Path(__file__).resolve().parent.parent  # 4EVR0-Server/
sys.path.insert(0, str(_APP_ROOT))
from app.clients.neo4j_client import query_ingredients_by_effects  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.domain.enums import Concern  # noqa: E402
from app.services.recommend_service import (  # noqa: E402
    _appropriate_categories,
    _requested_categories,
    apply_caution_filter,
    select_products,
)
from app.services.taxonomy_normalization_service import CONCERN_EFFECT_MAP as _REAL_CONCERN_EFFECT_MAP  # noqa: E402

# eval/gold_labels.py의 PRODUCTION_CONCERN_EFFECT_MAP은 이 원본을 손으로 베낀 사본인데
# (eval/이 다른 저장소라 그때는 import가 안 됐음), 실제로 4개 concern에서 이미 어긋나
# 있는 걸 발견했다(OILY_SKIN/HYPERPIGMENTATION/DULLNESS/WRINKLES). llm_query_eval은
# 4EVR0-Server 안에 있어서 원본을 바로 import할 수 있으므로 사본 대신 이걸 쓴다.
PRODUCTION_CONCERN_EFFECT_MAP: dict[str, list[str]] = {
    concern.value: [effect.value for effect in effects] for concern, effects in _REAL_CONCERN_EFFECT_MAP.items()
}

_GRAPHDB_ROOT = _APP_ROOT.parent  # /home/graphdb
sys.path.insert(0, str(_GRAPHDB_ROOT / "eval"))
from gold_labels import fetch_all_affects  # noqa: E402
from graphrag_ranking_eval import ndcg_at_k  # noqa: E402

from generate import GenerationError, generate_and_validate, get_client  # noqa: E402
from questions import SCENARIOS  # noqa: E402

A_INGREDIENT_HOPS = 1  # Ingredient-[:AFFECTS]->Effect
A_PRODUCT_HOPS = 2  # 위 + Product-[:CONTAINS]->Ingredient (별도 호출 2번이지만 순회 관계는 2종)

# 올리브영 review_stats의 "피부고민" 태그는 3개 대분류뿐 — 26개 concern 전체를 못 커버함.
_REVIEW_CONCERN_MAP: dict[str, set[str]] = {
    "보습에 좋아요": {"DRY_SKIN", "DEHYDRATED_SKIN", "FLAKY_SKIN", "BARRIER_DAMAGE"},
    "주름/미백에 좋아요": {
        "WRINKLES", "AGING_SIGNS", "LOSS_OF_ELASTICITY", "SAGGING_SKIN",
        "HYPERPIGMENTATION", "UNEVEN_SKIN_TONE", "DULLNESS", "BLEMISHES",
        "DARK_CIRCLES", "POST_ACNE_MARKS",
    },
    "진정에 좋아요": {
        "SENSITIVE_SKIN", "REDNESS", "IRRITATED_SKIN", "ATOPIC_PRONE", "ROSACEA_PRONE", "SUNBURN",
    },
}


def get_sync_driver():
    """실험 스크립트 전용 동기 드라이버 (B의 EXPLAIN 검증/실행, gold/리뷰 조회용).

    app.clients.neo4j_client는 자체 비동기 드라이버 싱글턴을 내부에서 관리하므로
    (A 호출은 그쪽을 그대로 씀), 이 드라이버와는 별개 커넥션이다.
    """
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def effects_for_concerns(concerns: list[str]) -> list[str]:
    """여러 concern의 effect를 합친다 (순서 유지, 중복 제거) — 프로덕션이 다중
    concern 메시지를 처리할 때 하는 것과 동일한 방식."""
    seen: dict[str, None] = {}
    for concern in concerns:
        for effect in PRODUCTION_CONCERN_EFFECT_MAP[concern]:
            seen.setdefault(effect, None)
    return list(seen)


def gold_ingredient_names(affects_df: pd.DataFrame, effects: list[str]) -> set[str]:
    candidates = affects_df[affects_df["effect_code"].isin(effects)]
    return set(candidates.loc[candidates["is_gold"], "inci_name"])


def score_ingredients(names: list[str], affects_df: pd.DataFrame, effects: list[str]) -> dict:
    """반환된 성분 이름 순서 리스트를 gold 기준(affects_df.is_gold)으로 채점한다.

    candidate_gold는 evaluate_concern()과 동일하게 (ingredient, effect) 행 기준으로
    센다 (이름으로 미리 dedup하면 recall 분모가 어긋남 — graphrag_ranking_eval.py 주석 참고).
    """
    candidates = affects_df[affects_df["effect_code"].isin(effects)]
    candidate_gold = int(candidates["is_gold"].sum())
    gold_names = gold_ingredient_names(affects_df, effects)

    returned_total = len(names)
    relevances = [1 if n in gold_names else 0 for n in names]
    gold_in_returned = sum(relevances)

    return {
        "returned": returned_total,
        "distinct": len(set(names)),
        "precision": gold_in_returned / returned_total if returned_total else None,
        "recall": gold_in_returned / candidate_gold if candidate_gold else None,
        "ndcg": ndcg_at_k(relevances) if returned_total else None,
        "candidate_gold": candidate_gold,
    }


def relevant_review_tag(concerns: list[str]) -> str | None:
    """concern들이 review_stats 3개 대분류 중 하나와 겹치면 그 태그, 아니면 None."""
    for tag, tag_concerns in _REVIEW_CONCERN_MAP.items():
        if tag_concerns & set(concerns):
            return tag
    return None


def _parse_review_pct(review_stats_raw, tag: str) -> float | None:
    if not review_stats_raw:
        return None
    try:
        data = json.loads(review_stats_raw)
        val = data.get("피부고민", {}).get(tag)
        return float(val.rstrip("%")) / 100 if val is not None else None
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


def score_products(
    products: list[dict], gold_names: set[str], concerns: list[str], message: str, sync_driver,
) -> dict:
    """제품 추천 품질: category_fit + ingredient_grounded + (해당 시) review_score."""
    n = len(products)
    if n == 0:
        return {"returned": 0, "category_fit": None, "ingredient_grounded": None,
                "review_tag": relevant_review_tag(concerns), "review_score": None}

    requested = _requested_categories(message)
    concerns_enum = [Concern(c) for c in concerns]
    appropriate = set(_appropriate_categories(concerns_enum, requested))
    category_fit = sum(1 for p in products if p.get("category") in appropriate) / n

    product_ids = [p["product_id"] for p in products if p.get("product_id")]
    grounded_ids: set[str] = set()
    review_by_id: dict[str, str | None] = {}
    if product_ids:
        with sync_driver.session() as session:
            rows = session.run(
                """
                UNWIND $pids AS pid
                MATCH (p:Product {product_id: pid})
                OPTIONAL MATCH (p)-[:CONTAINS]->(i:Ingredient) WHERE i.inci_name IN $gold_names
                WITH p, count(i) AS gold_count
                RETURN p.product_id AS product_id, gold_count, p.review_stats AS review_stats
                """,
                pids=product_ids, gold_names=list(gold_names),
            ).data()
        grounded_ids = {r["product_id"] for r in rows if r["gold_count"] > 0}
        review_by_id = {r["product_id"]: r["review_stats"] for r in rows}
    ingredient_grounded = (sum(1 for pid in product_ids if pid in grounded_ids) / n) if product_ids else 0.0

    tag = relevant_review_tag(concerns)
    review_score = None
    if tag:
        pcts = [p for p in (_parse_review_pct(review_by_id.get(pid), tag) for pid in product_ids) if p is not None]
        review_score = round(sum(pcts) / len(pcts), 4) if pcts else None

    return {
        "returned": n,
        "category_fit": round(category_fit, 4),
        "ingredient_grounded": round(ingredient_grounded, 4),
        "review_tag": tag,
        "review_score": review_score,
    }


def count_hops(cypher: str) -> int:
    """관계 순회(-[...]->) 개수를 정규식으로 근사 카운트한다. 대략적인 지표임."""
    return len(re.findall(r"-\s*\[", cypher))


async def evaluate_one(scenario: dict, affects_df, sync_driver, client, model) -> dict:
    concerns = scenario["concerns"]
    message = scenario["message"]
    effects = effects_for_concerns(concerns)
    concerns_enum = [Concern(c) for c in concerns]
    gold_names = gold_ingredient_names(affects_df, effects)
    row: dict = {"id": scenario["id"], "message": message, "concerns": concerns, "effects": effects}

    # A — 프로덕션 파이프라인 그대로: 성분 조회 → CAUTION 필터 → 제품 선정
    a_ingredients_raw = await query_ingredients_by_effects(effects, min_graph_score=settings.ingredient_min_graph_score)
    a_ingredients_raw = await apply_caution_filter(a_ingredients_raw, concerns_enum)
    row["a_ingredients"] = score_ingredients([r["name"] for r in a_ingredients_raw], affects_df, effects)
    row["a_ingredients"]["hops"] = A_INGREDIENT_HOPS

    a_ingredient_scores = [
        {"name": r["name"], "weight": float(r.get("graph_score") or 1.0)} for r in a_ingredients_raw[:10]
    ]
    a_products = await select_products(message, concerns_enum, a_ingredient_scores)
    row["a_products"] = score_products(a_products, gold_names, concerns, message, sync_driver)
    row["a_products"]["hops"] = A_PRODUCT_HOPS

    # B — LLM은 concerns/effects를 미리 안 받고, 원문 메시지만 보고 제품까지 직접 조회
    try:
        gen = await generate_and_validate(
            message, client, sync_driver, model, prompt_name="cypher_generation_products",
        )
        b_rows = gen["rows"]  # generate_and_validate가 이미 실제로 실행해서 받아둔 결과 (재실행 안 함)
        if "product_id" not in b_rows[0]:
            raise GenerationError(f"결과에 'product_id' 컬럼 없음: {list(b_rows[0].keys())}")
        row["b_products"] = score_products(b_rows, gold_names, concerns, message, sync_driver)
        row["b_products"]["hops"] = count_hops(gen["cypher"])
        row["b_products"]["attempts"] = gen["attempts"]
        row["b_products"]["failed"] = False
        row["b_products"]["cypher"] = gen["cypher"]
        row["b_products"]["params"] = gen["params"]
    except Exception as exc:  # noqa: BLE001 — 생성/검증/실행 실패를 전부 "실패" 케이스로 집계
        row["b_products"] = {"failed": True, "error": f"{type(exc).__name__}: {exc}"}

    return row


async def run(limit: int | None) -> list[dict]:
    all_concerns = {c for s in SCENARIOS for c in s["concerns"]}
    unknown = all_concerns - set(PRODUCTION_CONCERN_EFFECT_MAP)
    assert not unknown, f"dataset.jsonl에 PRODUCTION_CONCERN_EFFECT_MAP에 없는 concern이 있음: {unknown}"

    sync_driver = get_sync_driver()
    client = get_client()
    model = settings.gpu_model

    try:
        affects_df = fetch_all_affects(sync_driver)
        scenarios = SCENARIOS[:limit] if limit else SCENARIOS

        results = []
        for scenario in scenarios:
            t0 = time.perf_counter()
            row = await evaluate_one(scenario, affects_df, sync_driver, client, model)
            row["elapsed_s"] = round(time.perf_counter() - t0, 2)
            results.append(row)
            b = row["b_products"]
            b_summary = "FAILED" if b.get("failed") else (
                f"cat_fit={b['category_fit']:.2f} grounded={b['ingredient_grounded']:.2f}"
                f" review={b['review_score']} hops={b['hops']}"
            )
            a = row["a_products"]
            label = "+".join(row["concerns"])
            print(f"[id {row['id']:>2} {label[:30]:30s}] A: cat_fit={a['category_fit']:.2f}"
                  f" grounded={a['ingredient_grounded']:.2f} review={a['review_score']}"
                  f"  |  B: {b_summary}  ({row['elapsed_s']}s)")
        return results
    finally:
        sync_driver.close()


def summarize(results: list[dict]) -> dict:
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    b_ok = [r for r in results if not r["b_products"].get("failed")]
    return {
        "n_cases": len(results),
        "b_failure_rate": round(1 - len(b_ok) / len(results), 4) if results else None,
        "a_category_fit": avg(r["a_products"]["category_fit"] for r in results),
        "a_ingredient_grounded": avg(r["a_products"]["ingredient_grounded"] for r in results),
        "a_review_score": avg(r["a_products"]["review_score"] for r in results),
        "a_hops": avg(r["a_products"]["hops"] for r in results),
        "b_category_fit": avg(r["b_products"]["category_fit"] for r in b_ok),
        "b_ingredient_grounded": avg(r["b_products"]["ingredient_grounded"] for r in b_ok),
        "b_review_score": avg(r["b_products"]["review_score"] for r in b_ok),
        "b_hops": avg(r["b_products"]["hops"] for r in b_ok),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="시나리오 N개만 처리 (파이프라인 확인용)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    results = asyncio.run(run(args.limit))
    summary = summarize(results)

    print("\n" + "=" * 70)
    print("요약 (B 실패 케이스는 b_* 평균에서 제외, b_failure_rate로 별도 집계)")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:20s} {v}")

    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parent / "results" / f"ab_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"run": {"timestamp": datetime.now(timezone.utc).isoformat()}, "summary": summary, "cases": results},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n결과 저장: {out}")


if __name__ == "__main__":
    main()
