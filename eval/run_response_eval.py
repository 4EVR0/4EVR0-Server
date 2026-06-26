"""응답 품질 평가 (LLM-as-judge).

추출 평가(run_eval.py)가 못 보는 **생성된 추천문 자체의 품질**을 측정한다.
각 메시지에 대해 실제 추천 파이프라인(recommend_service.recommend)을 돌려 응답을 만들고,
그 응답을 LLM 심판(judge)이 루브릭으로 1~5점 채점한다.

전제: Neo4j(EC2) + vLLM 가동. (HTTP/세션 불필요 — 서비스 함수 직접 호출)

사용:
    python eval/run_response_eval.py
    python eval/run_response_eval.py --limit 5

⚠️ 한계:
- 심판이 생성과 **같은 모델**(self-eval)이면 자기선호 편향 가능 → 결과는 상대 비교용으로 해석.
- 응답 생성 temperature>0 이면 run마다 약간 변동. (현재 production 동작을 그대로 평가)
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.clients.llm_factory import get_async_llm_client  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.prompts import load_prompt, prompt_version  # noqa: E402
from app.services.recommend_service import recommend  # noqa: E402

JUDGE_PROMPT_NAME = "response_judge"
GEN_PROMPT_NAME = "recommend_response"  # 평가 대상이 되는 응답 생성 프롬프트
DIMS = ["concern_fit", "grounding", "conciseness", "korean_quality", "format_adherence"]


def load_dataset(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


async def judge_response(client, message, ingredients, products, response, judge_prompt) -> dict:
    """응답을 심판 LLM에게 채점받아 dict 반환."""
    ing_lines = "\n".join(f"- {i.name}: {i.claim or '효능 데이터 없음'}" for i in ingredients[:10]) or "(없음)"
    prod_lines = "\n".join(
        f"- [{p.category}] {p.brand} {p.product_name} (핵심성분 {p.matched_count}개)" for p in products
    ) or "(없음)"
    content = (
        f"[User message]\n{message}\n\n"
        f"[Provided ingredients]\n{ing_lines}\n\n"
        f"[Provided products]\n{prod_lines}\n\n"
        f"[Assistant response]\n{response}"
    )
    resp = await client.chat.completions.create(
        model=settings.gpu_model,
        messages=[
            {"role": "system", "content": judge_prompt},
            {"role": "user", "content": content},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    # 1~5 범위로 클램프 + 누락 방지
    scores = {}
    for d in DIMS:
        try:
            scores[d] = max(1, min(5, int(round(float(data.get(d))))))
        except (TypeError, ValueError):
            scores[d] = None
    scores["comment"] = str(data.get("comment", ""))[:200]
    return scores


async def run(dataset_path: Path, limit: int | None) -> dict:
    cases = load_dataset(dataset_path)
    if limit:
        cases = cases[:limit]
    client = get_async_llm_client()
    judge_prompt = load_prompt(JUDGE_PROMPT_NAME)

    results = []
    dim_scores = {d: [] for d in DIMS}
    errors = 0
    gen_latencies = []

    for case in cases:
        row = {"id": case["id"], "label": case.get("label", ""), "message": case["message"]}
        try:
            t0 = time.perf_counter()
            rec = await recommend("eval-response", case["message"])  # 실제 파이프라인 (Neo4j+vLLM)
            gen_latencies.append(time.perf_counter() - t0)
            scores = await judge_response(
                client, case["message"], rec.ingredients, rec.products, rec.response_text, judge_prompt
            )
        except Exception as exc:
            errors += 1
            row["error"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
            print(f"  [id {case['id']:>2}] ERROR {type(exc).__name__}: {exc}")
            continue

        valid = [scores[d] for d in DIMS if scores[d] is not None]
        for d in DIMS:
            if scores[d] is not None:
                dim_scores[d].append(scores[d])
        overall = round(statistics.mean(valid), 2) if valid else None
        row.update({
            "scores": {d: scores[d] for d in DIMS}, "overall": overall,
            "comment": scores["comment"],
            "n_products": len(rec.products), "n_ingredients": len(rec.ingredients),
            "response": rec.response_text,
        })
        results.append(row)
        _abbr = {"concern_fit": "fit", "grounding": "grnd", "conciseness": "concise",
                 "korean_quality": "kor", "format_adherence": "fmt"}
        print(f"  [id {case['id']:>2}] overall={overall}  " +
              " ".join(f"{_abbr[d]}={scores[d]}" for d in DIMS))

    scored = len(cases) - errors
    metrics = {f"resp_{d}": round(statistics.mean(dim_scores[d]), 3) for d in DIMS if dim_scores[d]}
    all_means = [statistics.mean(dim_scores[d]) for d in DIMS if dim_scores[d]]
    metrics["resp_overall"] = round(statistics.mean(all_means), 3) if all_means else None
    metrics["error_rate"] = round(errors / len(cases), 4) if cases else 0.0
    metrics["gen_latency_p50"] = round(statistics.median(gen_latencies), 2) if gen_latencies else None

    run_info = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": settings.gpu_model,
        "gen_prompt": GEN_PROMPT_NAME,
        "gen_prompt_version": prompt_version(GEN_PROMPT_NAME),
        "judge_prompt_version": prompt_version(JUDGE_PROMPT_NAME),
        "dataset": str(dataset_path),
        "n_cases": len(cases),
        "n_scored": scored,
    }
    return {"run": run_info, "metrics": metrics, "cases": results}


def print_summary(report: dict) -> None:
    r, m = report["run"], report["metrics"]
    print("\n" + "═" * 60)
    print("  응답 품질 평가 (LLM-judge)")
    print("═" * 60)
    print(f"  model={r['model']}  gen_prompt={r['gen_prompt_version']}  judge={r['judge_prompt_version']}")
    print(f"  n={r['n_cases']} (scored {r['n_scored']})")
    print("─" * 60)
    for d in DIMS:
        if f"resp_{d}" in m:
            print(f"  {d:<20} {m['resp_' + d]:.2f} / 5")
    print("─" * 60)
    print(f"  {'OVERALL':<20} {m.get('resp_overall')} / 5")
    print(f"  {'에러율':<20} {m['error_rate']}")
    print(f"  {'응답생성 p50':<20} {m.get('gen_latency_p50')}s")
    print("═" * 60)


def log_to_mlflow(report: dict, artifact_path: Path | None) -> None:
    try:
        import mlflow
    except ImportError:
        print("  (mlflow 미설치 — 기록 건너뜀)")
        return
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{_REPO_ROOT / 'eval' / 'mlflow.db'}")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("4evr0-response-quality")
    run, metrics = report["run"], report["metrics"]
    with mlflow.start_run(run_name=run["timestamp"]):
        mlflow.log_params({k: run[k] for k in
                           ("model", "gen_prompt", "gen_prompt_version", "judge_prompt_version", "n_cases", "n_scored")})
        mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
        if artifact_path:
            mlflow.log_artifact(str(artifact_path))
    print(f"  MLflow 기록: experiment='4evr0-response-quality' @ {tracking_uri}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(_REPO_ROOT / "eval" / "dataset.jsonl"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-mlflow", action="store_true")
    args = ap.parse_args()

    report = asyncio.run(run(Path(args.dataset), args.limit))
    print_summary(report)

    out = Path(args.out) if args.out else _REPO_ROOT / "eval" / "results" / f"resp-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")

    if not args.no_mlflow:
        log_to_mlflow(report, out)


if __name__ == "__main__":
    main()
