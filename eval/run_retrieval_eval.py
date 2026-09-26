"""검색(RAG) 품질 eval — Neo4j 검색 자체의 precision (이슈 #40).

추출·생성과 분리해 **검색만** 격리 측정한다. gold profile을 사용하되 제품 선택은
운영과 같은 경로를 재사용한다:
    gold concern → infer_effects → query_ingredients_by_effects → caution filter
    → select_products(30개 후보·목적 필터·재정렬·다양화) → constraint evidence guard
(추출 LLM을 안 타므로 추출 오류가 검색 점수를 오염시키지 않는다.)

reference-free LLM-judge로 "검색된 성분/제품이 이 고민에 관련 있나"를 항목별 판정 → precision@k.
골드 라벨(기대 성분/제품) 없이도 도는 게 장점(#40 §2-(c)).

측정 지표:
    - product_precision  : 검색된 제품 중 고민에 관련 있는 비율 (★ 핵심 — 성분 겹침으로 무관 제품 딸려오는 문제)
    - ingredient_precision: 검색된 성분 중 관련 있는 비율
    - product_zero_rate / ingredient_zero_rate : 전체 빈손 검색 비율(관측용)
    - unexpected_product_zero_rate: 고민이 있고 미검증 제품 제약이 없는 케이스의 빈손 비율
    - evidence_top_ratio : 상위 성분 중 pubmed_evidence 비율 (랭킹 품질)

사용:
    JUDGE_MODEL=gpt-4o-mini JUDGE_API_KEY=<key> python eval/run_retrieval_eval.py [--limit N]
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import openai

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.domain.enums import Concern, Constraint  # noqa: E402
from app.services.taxonomy_normalization_service import infer_effects  # noqa: E402
from app.services.recommend_service import (  # noqa: E402
    _apply_constraint_evidence_guard,
    apply_caution_filter,
    select_products,
)
from app.clients.neo4j_client import (  # noqa: E402
    close_driver,
    ping,
    query_ingredients_by_effects,
)
from eval.eval_utils import bootstrap_mean_ci, file_sha256, git_code_sha, load_dataset  # noqa: E402
from eval.mlflow_tracking import log_report  # noqa: E402
from eval.run_response_eval import build_judge_config, build_judge_client  # noqa: E402

_TOP_INGREDIENTS = 8   # judge에 보낼 성분 상위 수
_JUDGE_PROMPT = """당신은 화장품 추천 시스템의 **검색 품질 평가자**입니다.
사용자 고민에 대해 검색 시스템이 반환한 '성분'과 '제품'이 **그 고민에 실제로 관련 있는지** 항목별로 판정하세요.
- 제품은 이름·성분으로 판단: 그 고민을 위한 제품이면 1, 목적이 다른 제품(예: 여드름 고민인데 '기미/미백' 제품)이면 0.
- 다중 고민에서 제품 하나가 모든 고민을 동시에 해결할 필요는 없습니다. 명시된 고민 중 하나 이상에 직접 맞고 다른 고민과 명백히 충돌하지 않으면 1입니다.
- 성분은 그 고민 개선에 쓰이는 성분이면 1, 무관하면 0.

사용자 고민: "{message}"
(고민 코드: {concerns})

[검색된 성분]
{ingredients}

[검색된 제품]
{products}

각 항목을 순서대로 1(관련)/0(무관)으로. **JSON만** 출력:
{{"ingredients": [...], "products": [...]}}"""


def _parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.DOTALL)
    return json.loads(m.group(0) if m else t)


def _candidate_snapshots(ingredients: list[dict], products: list[dict]) -> tuple[list[dict], list[dict]]:
    """Persist the ordered judge inputs so later score changes can be audited."""
    ingredient_rows = [
        {
            "rank": rank,
            "name": row["name"],
            "eligibility_tier": row.get("eligibility_tier"),
            "graph_score": row.get("graph_score"),
            "judge_relevant": None,
        }
        for rank, row in enumerate(ingredients, start=1)
    ]
    product_rows = [
        {
            "rank": rank,
            "product_id": row.get("product_id"),
            "product_name": row.get("product_name"),
            "category": row.get("category"),
            "matched_ingredients": row.get("matched_ingredients") or [],
            "judge_relevant": None,
        }
        for rank, row in enumerate(products, start=1)
    ]
    return ingredient_rows, product_rows


def _validated_flags(verdict: dict, key: str, expected: int) -> list[int]:
    flags = verdict.get(key)
    if (not isinstance(flags, list) or len(flags) != expected
            or any(type(flag) is not int or flag not in (0, 1) for flag in flags)):
        raise ValueError(f"judge {key}: expected exactly {expected} binary decisions")
    return flags


async def _judge(client, model, timeout, message, concerns, ing_names, products) -> dict:
    ing_lines = "\n".join(f"{i+1}. {n}" for i, n in enumerate(ing_names)) or "(없음)"
    prod_lines = "\n".join(
        f"{i+1}. [{p.get('category')}] {p.get('product_name')} — 매칭성분: {p.get('matched_ingredients')}"
        for i, p in enumerate(products)
    ) or "(없음)"
    prompt = _JUDGE_PROMPT.format(message=message, concerns=", ".join(concerns),
                                 ingredients=ing_lines, products=prod_lines)
    resp = await client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json(resp.choices[0].message.content)


async def eval_case(case, judge_client, judge_model, judge_timeout, *, capture_only=False) -> dict:
    concerns = [Concern(c) for c in case.get("concerns", []) if c in Concern._value2member_map_]
    effects = infer_effects(concerns)
    raw_ings = await query_ingredients_by_effects(
        [e.value for e in effects], min_graph_score=settings.ingredient_min_graph_score)
    # 운영 추천과 동일하게 민감성 계열 요청의 주의 성분을 먼저 제거한다. 이 단계가 빠지면
    # 평가기만 다른 상위 성분·제품을 보게 되어 실제 서비스에는 없는 검색 공백이 생긴다.
    raw_ings = await apply_caution_filter(raw_ings, concerns)
    scores = [{"name": r["name"], "weight": float(r.get("graph_score") or 1.0)} for r in raw_ings[:10]]
    # 실제 서비스는 기본 query limit(5)이 아니라 30개 후보를 확보한 뒤 목적 필터·리뷰
    # 재정렬·카테고리 다양화를 적용한다. 평가기도 같은 함수를 재사용해 경로 드리프트를 막는다.
    products = await select_products(case["message"], concerns, scores)
    constraints = [
        Constraint(code)
        for code in case.get("constraints", [])
        if code in Constraint._value2member_map_
    ]
    products = _apply_constraint_evidence_guard(products, constraints)
    judged_ings = raw_ings[:_TOP_INGREDIENTS]
    ing_names = [r["name"] for r in judged_ings]
    ingredient_candidates, product_candidates = _candidate_snapshots(judged_ings, products)

    # 랭킹 품질: 상위 성분 중 pubmed_evidence 비율
    top = raw_ings[:_TOP_INGREDIENTS]
    ev_ratio = (sum(1 for r in top if r.get("eligibility_tier") == "pubmed_evidence") / len(top)) if top else None

    result = {
        "id": case.get("id"), "concerns": [c.value for c in concerns],
        "n_ingredients": len(raw_ings), "n_products": len(products),
        # 고민 없음 또는 현재 제품 속성 데이터로 검증할 수 없는 제약 요청은
        # 제품 0건이 안전한 정상 결과다. 회귀 게이트의 검색 공백에서 제외한다.
        "expects_products": bool(concerns) and not bool(constraints),
        "evidence_top_ratio": ev_ratio,
        "ingredient_precision": None, "product_precision": None,
        "ingredient_candidates": ingredient_candidates,
        "product_candidates": product_candidates,
    }
    if capture_only or (not ing_names and not products):
        return result  # 캡처 전용 또는 둘 다 빈손 — judge 스킵

    verdict = await _judge(judge_client, judge_model, judge_timeout,
                           case["message"], result["concerns"], ing_names, products)
    ing_flags = _validated_flags(verdict, "ingredients", len(ing_names))
    prod_flags = _validated_flags(verdict, "products", len(products))
    for row, flag in zip(ingredient_candidates, ing_flags):
        row["judge_relevant"] = flag
    for row, flag in zip(product_candidates, prod_flags):
        row["judge_relevant"] = flag
    if ing_names:
        result["ingredient_precision"] = sum(ing_flags) / len(ing_names)
    if products:
        result["product_precision"] = sum(prod_flags) / len(products)
        result["irrelevant_products"] = [products[i].get("product_name")
                                         for i, f in enumerate(prod_flags) if f == 0]
    return result


def _agg(cases, key):
    vals = [c[key] for c in cases if c.get(key) is not None]
    if not vals:
        return None
    ci = bootstrap_mean_ci(vals)  # (lo, hi) 또는 None
    return {"mean": round(statistics.mean(vals), 4),
            "ci95": [ci[0], ci[1]] if ci else None, "n": len(vals)}


def _unexpected_product_zero_rate(cases: list[dict]) -> float:
    expected = [case for case in cases if case.get("expects_products")]
    if not expected:
        return 0.0
    return round(sum(case.get("n_products") == 0 for case in expected) / len(expected), 4)


async def main_async(args) -> None:
    dataset_path = Path(args.dataset)
    cases = load_dataset(dataset_path)
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if str(case["id"]) in selected]
        missing = selected - {str(case["id"]) for case in cases}
        if missing:
            raise ValueError(f"dataset missing case ids: {', '.join(sorted(missing))}")
    if args.limit:
        cases = cases[: args.limit]

    # Graph client queries intentionally return [] on connection errors for serving.
    # Evaluation must fail closed, or an outage is silently reported as zero recall.
    try:
        await ping()
    except Exception as exc:
        raise RuntimeError("Neo4j unavailable; retrieval report was not created") from exc

    jc = None
    client = None
    if not args.capture_only:
        jc = build_judge_config(model=args.judge_model, base_url=None,
                                api_key_env=args.judge_api_key_env,
                                timeout_seconds=args.judge_timeout, allow_self_judge=False)
        client = build_judge_client(jc)

    sem = asyncio.Semaphore(args.concurrency)

    async def _run(case):
        async with sem:
            try:
                r = await eval_case(case, client, jc.model if jc else None,
                                    jc.timeout_seconds if jc else None,
                                    capture_only=args.capture_only)
                pp = r.get("product_precision")
                print(f"  [id {r['id']}] ing={r['n_ingredients']} prod={r['n_products']} "
                      f"prod_prec={pp if pp is None else round(pp,2)}", flush=True)
                return r
            except Exception as exc:
                print(f"  [id {case.get('id')}] 실패: {exc}", flush=True)
                return {"id": case.get("id"), "error": str(exc)}

    results = await asyncio.gather(*[_run(c) for c in cases])
    await close_driver()
    ok = [r for r in results if "error" not in r]

    n = len(ok)
    metrics = {
        "product_precision": _agg(ok, "product_precision"),
        "ingredient_precision": _agg(ok, "ingredient_precision"),
        "evidence_top_ratio": _agg(ok, "evidence_top_ratio"),
        "product_zero_rate": round(sum(1 for r in ok if r["n_products"] == 0) / n, 4) if n else None,
        "unexpected_product_zero_rate": _unexpected_product_zero_rate(ok),
        "ingredient_zero_rate": round(sum(1 for r in ok if r["n_ingredients"] == 0) / n, 4) if n else None,
        "mean_products_found": round(statistics.mean([r["n_products"] for r in ok]), 2) if n else None,
        "error_rate": round((len(results) - n) / len(results), 4) if results else 0,
    }
    report = {
        "run": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "code_sha": git_code_sha(),
            "judge_model": jc.model if jc else None, "judge_mode": "capture_only" if args.capture_only else "judged",
            "judge_prompt_sha256": hashlib.sha256(_JUDGE_PROMPT.encode("utf-8")).hexdigest(),
            "candidate_trace_schema": 1, "dataset": str(dataset_path),
            "dataset_sha256": file_sha256(dataset_path), "n_cases": len(cases), "n_scored": n,
            "product_min_relevance_ratio": settings.product_min_relevance_ratio,
            "product_min_matched_count": settings.product_min_matched_count,
            "ingredient_min_graph_score": settings.ingredient_min_graph_score,
        },
        "metrics": metrics,
        "cases": ok,
        "errors": [r for r in results if "error" in r],
    }
    print("\n" + "═" * 60)
    print("  검색(RAG) 품질 eval")
    print("═" * 60)
    for k in ("product_precision", "ingredient_precision", "evidence_top_ratio"):
        m = metrics[k]
        print(f"  {k:<22} {m['mean'] if m else '—'}"
              + (f" (95% CI {m['ci95'][0]}–{m['ci95'][1]}, n={m['n']})" if m and m['ci95'] else ""))
    print(f"  {'product_zero_rate':<22} {metrics['product_zero_rate']}")
    print(f"  {'unexpected_zero_rate':<22} {metrics['unexpected_product_zero_rate']}")
    print(f"  {'ingredient_zero_rate':<22} {metrics['ingredient_zero_rate']}")
    print("═" * 60)

    out = Path(args.out) if args.out else (_REPO_ROOT / "eval" / "results"
                                           / f"retrieval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"결과 저장: {out}")
    if not args.no_mlflow and not args.capture_only:
        status, run_id = log_report(out)
        print(f"  MLflow {status}: {run_id}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(_REPO_ROOT / "eval" / "dataset.jsonl"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--case-id", action="append", help="특정 ID만 평가 (반복 지정 가능)")
    ap.add_argument("--capture-only", action="store_true", help="외부 Judge 없이 후보 순서만 로컬에 저장; MLflow 기록 없음")
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--judge-api-key-env", default="JUDGE_API_KEY")
    ap.add_argument("--judge-timeout", type=float, default=120.0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-mlflow", action="store_true", help="MLflow 기록 비활성화")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
