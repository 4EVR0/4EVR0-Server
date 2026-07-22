"""A(프로덕션 고정 Cypher) vs B(LLM 생성 Cypher) 품질 비교.

eval/dataset.jsonl의 시나리오(여러 고민이 한 문장에 섞인 실제 사용자 발화
스타일, questions.py가 그대로 읽어옴)마다:
  - A: app.clients.neo4j_client.query_ingredients_by_effects를 **직접 호출**
    (텍스트를 복사하지 않음 — 복사본은 원본과 어긋날 수 있고, 실제로 이번 작업
    중 pg_experiment/queries.py와 eval/graphrag_ranking_eval.py의 복사본이
    프로덕션과 어긋나 있는 걸 발견했음). 시나리오의 concerns 전체를 합친
    effect 집합으로 호출 — 프로덕션이 다중 concern일 때 하는 것과 동일.
  - B: LLM이 원문 메시지만 보고 생성한 Cypher(EXPLAIN 검증 통과분)를 실행
정답 기준은 eval/gold_labels.py의 AFFECTS 엣지 evidence_type == 'pubmed_evidence'
(그래프 자체의 근거 강도 프록시). A/B 모두 같은 기준으로 precision/recall/ndcg 채점.

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

_GRAPHDB_ROOT = _APP_ROOT.parent  # /home/graphdb
sys.path.insert(0, str(_GRAPHDB_ROOT / "eval"))
from gold_labels import PRODUCTION_CONCERN_EFFECT_MAP, fetch_all_affects  # noqa: E402
from graphrag_ranking_eval import ndcg_at_k  # noqa: E402

from generate import GenerationError, generate_and_validate, get_client  # noqa: E402
from questions import SCENARIOS  # noqa: E402

# A는 Ingredient-[:AFFECTS]->Effect 단일 hop 구조로 고정돼 있음
# (query_ingredients_by_effects의 구조적 사실 — 매 호출 텍스트를 파싱할 필요 없음).
A_HOPS = 1


def get_sync_driver():
    """실험 스크립트 전용 동기 드라이버 (B의 EXPLAIN 검증/실행, gold 후보 조회용).

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


def score_ingredients(names: list[str], affects_df: pd.DataFrame, effects: list[str]) -> dict:
    """반환된 성분 이름 순서 리스트를 gold 기준(affects_df.is_gold)으로 채점한다.

    candidate_gold는 evaluate_concern()과 동일하게 (ingredient, effect) 행 기준으로
    센다 (이름으로 미리 dedup하면 recall 분모가 어긋남 — graphrag_ranking_eval.py 주석 참고).
    """
    candidates = affects_df[affects_df["effect_code"].isin(effects)]
    candidate_gold = int(candidates["is_gold"].sum())
    gold_names = set(candidates.loc[candidates["is_gold"], "inci_name"])

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


def count_hops(cypher: str) -> int:
    """관계 순회(-[...]->) 개수를 정규식으로 근사 카운트한다. 대략적인 지표임."""
    return len(re.findall(r"-\s*\[", cypher))


async def evaluate_one(scenario: dict, affects_df, sync_driver, client, model) -> dict:
    concerns = scenario["concerns"]
    effects = effects_for_concerns(concerns)
    row: dict = {
        "id": scenario["id"], "message": scenario["message"], "concerns": concerns, "effects": effects,
    }

    # A — 프로덕션 함수를 그대로 호출 (min_graph_score도 프로덕션 기본값 그대로)
    a_rows = await query_ingredients_by_effects(effects, min_graph_score=settings.ingredient_min_graph_score)
    a_names = [r["name"] for r in a_rows]
    row["a"] = score_ingredients(a_names, affects_df, effects)
    row["a"]["hops"] = A_HOPS

    # B — LLM은 concerns/effects를 미리 안 받고, 원문 메시지만 보고 스스로 판단
    try:
        gen = await generate_and_validate(scenario["message"], client, sync_driver, model)
        with sync_driver.session() as session:
            b_rows = session.run(gen["cypher"], **gen["params"]).data()
        if not b_rows or "name" not in b_rows[0]:
            raise GenerationError(f"결과에 'name' 컬럼 없음: {list(b_rows[0].keys()) if b_rows else '결과 없음'}")
        b_names = [r["name"] for r in b_rows]
        row["b"] = score_ingredients(b_names, affects_df, effects)
        row["b"]["hops"] = count_hops(gen["cypher"])
        row["b"]["attempts"] = gen["attempts"]
        row["b"]["failed"] = False
        row["b"]["cypher"] = gen["cypher"]
        row["b"]["params"] = gen["params"]
    except Exception as exc:  # noqa: BLE001 — 생성/검증/실행 실패를 전부 "실패" 케이스로 집계
        row["b"] = {"failed": True, "error": f"{type(exc).__name__}: {exc}"}

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
            b = row["b"]
            b_summary = "FAILED" if b.get("failed") else (
                f"P={b['precision']:.2f} R={b['recall']:.2f} NDCG={b['ndcg']:.2f} hops={b['hops']}"
            )
            a = row["a"]
            label = "+".join(row["concerns"])
            print(f"[id {row['id']:>2} {label[:30]:30s}] A: P={a['precision']:.2f} R={a['recall']:.2f}"
                  f" NDCG={a['ndcg']:.2f}  |  B: {b_summary}  ({row['elapsed_s']}s)")
        return results
    finally:
        sync_driver.close()


def summarize(results: list[dict]) -> dict:
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    b_ok = [r for r in results if not r["b"].get("failed")]
    return {
        "n_cases": len(results),
        "b_failure_rate": round(1 - len(b_ok) / len(results), 4) if results else None,
        "a_precision": avg(r["a"]["precision"] for r in results),
        "a_recall": avg(r["a"]["recall"] for r in results),
        "a_ndcg": avg(r["a"]["ndcg"] for r in results),
        "a_hops": avg(r["a"]["hops"] for r in results),
        "b_precision": avg(r["b"]["precision"] for r in b_ok),
        "b_recall": avg(r["b"]["recall"] for r in b_ok),
        "b_ndcg": avg(r["b"]["ndcg"] for r in b_ok),
        "b_hops": avg(r["b"]["hops"] for r in b_ok),
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
