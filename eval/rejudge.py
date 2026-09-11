"""저장된 응답을 다른 루브릭으로 재채점 — 루브릭 변경 효과만 분리 측정.

루브릭을 고친 뒤 전체 평가를 다시 돌리면 **생성 비결정성이 섞인다**(vLLM은 temperature 0
에서도 배치·커널 비결정성 때문에 완전히 결정적이지 않다). 그러면 점수 변화가 루브릭 때문인지
응답이 달라져서인지 구분할 수 없다.

이 스크립트는 리포트에 저장된 `response`와 `evidence`를 그대로 다시 채점한다.
입력이 동일하므로 차이는 **루브릭 변경 효과뿐**이다. GPU도 Neo4j도 필요 없다.

사용:
    python eval/rejudge.py --report eval/results/v7-baseline.json \
      --judge-prompt response_judge --out eval/results/v7-rejudged.json

    # 두 채점 결과 비교
    python eval/rejudge.py --compare eval/results/v7-baseline.json eval/results/v7-rejudged.json
"""

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.prompts import load_prompt, prompt_version  # noqa: E402
from eval.eval_utils import bootstrap_mean_ci  # noqa: E402
from eval.run_response_eval import (  # noqa: E402
    DIMS,
    JUDGE_PROMPT_NAME,
    build_judge_client,
    build_judge_config,
    find_hanja,
    judge_with_evidence,
)


def load_scorable(report: dict) -> list[dict]:
    """재채점 가능한 케이스 — 응답과 근거 컨텍스트가 모두 있어야 한다."""
    cases = [
        c for c in report.get("cases", [])
        if c.get("response") and c.get("evidence") and not c.get("error")
    ]
    if not cases:
        raise ValueError(
            "재채점할 케이스가 없습니다. evidence 필드가 있는 리포트가 필요합니다"
            "(해당 필드 도입 이전 리포트는 재채점 불가)"
        )
    return cases


async def rejudge(report_path: Path, judge_config, judge_prompt_name: str,
                  judge_repeats: int, bootstrap_samples: int, seed: int) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cases = load_scorable(report)
    client = build_judge_client(judge_config)
    judge_prompt = load_prompt(judge_prompt_name)

    results, dim_scores = [], {d: [] for d in DIMS}
    repeat_stddevs: list[float] = []

    for case in cases:
        runs = [
            await judge_with_evidence(
                client, judge_config.model, case["message"],
                case["evidence"], case["response"], judge_prompt,
            )
            for _ in range(judge_repeats)
        ]
        scores = {
            dim: round(statistics.mean(r[dim] for r in runs if r[dim] is not None), 3)
            if any(r[dim] is not None for r in runs) else None
            for dim in DIMS
        }
        if judge_repeats > 1:
            repeat_stddevs.extend(
                statistics.pstdev([r[dim] for r in runs if r[dim] is not None])
                for dim in DIMS if any(r[dim] is not None for r in runs)
            )
        valid = [scores[d] for d in DIMS if scores[d] is not None]
        overall = round(statistics.mean(valid), 2) if valid else None
        for d in DIMS:
            if scores[d] is not None:
                dim_scores[d].append(scores[d])
        # 원본 케이스를 보존하되 점수만 새 것으로 교체(응답·근거는 그대로).
        # 한자 검사는 응답에서 다시 계산 — 옛 리포트에 이 필드가 없어도 채워진다.
        row = {**case, "scores": scores, "overall": overall, "comment": runs[0]["comment"],
               "hanja": find_hanja(case["response"])}
        results.append(row)
        print(f"  [id {case['id']:>2}] overall={overall}  " +
              " ".join(f"{d}={scores[d]}" for d in DIMS))

    metrics = {f"resp_{d}": round(statistics.mean(dim_scores[d]), 3) for d in DIMS if dim_scores[d]}
    for index, dim in enumerate(DIMS):
        ci = bootstrap_mean_ci(dim_scores[dim], samples=bootstrap_samples, seed=seed + index)
        if ci:
            metrics[f"resp_{dim}_ci95_low"], metrics[f"resp_{dim}_ci95_high"] = ci
    means = [statistics.mean(dim_scores[d]) for d in DIMS if dim_scores[d]]
    metrics["resp_overall"] = round(statistics.mean(means), 3) if means else None
    overalls = [r["overall"] for r in results if r.get("overall") is not None]
    ci = bootstrap_mean_ci(overalls, samples=bootstrap_samples, seed=seed)
    if ci:
        metrics["resp_overall_ci95_low"], metrics["resp_overall_ci95_high"] = ci
    metrics["judge_repeat_stddev"] = (
        round(statistics.mean(repeat_stddevs), 4) if repeat_stddevs else 0.0
    )
    leaks = [r for r in results if r.get("hanja")]
    metrics["hanja_leak_cases"] = len(leaks)
    metrics["hanja_leak_rate"] = round(len(leaks) / len(results), 4) if results else 0.0

    run_info = {
        **report.get("run", {}),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": judge_config.model,
        "judge_base_url": judge_config.base_url,
        "judge_prompt": judge_prompt_name,
        "judge_prompt_version": prompt_version(judge_prompt_name),
        "judge_repeats": judge_repeats,
        "n_cases": len(cases),
        "n_scored": len(results),
        # 재채점이라는 사실과 출처를 남긴다 — 생성 조건은 원본 리포트의 것이다.
        "rejudged_from": report_path.name,
        "rejudged_from_judge_prompt_version": report.get("run", {}).get("judge_prompt_version"),
    }
    return {"run": run_info, "metrics": metrics, "cases": results}


def compare(left_path: Path, right_path: Path) -> int:
    left = json.loads(left_path.read_text(encoding="utf-8"))
    right = json.loads(right_path.read_text(encoding="utf-8"))
    lc = {c["id"]: c for c in left.get("cases", []) if c.get("scores")}
    rc = {c["id"]: c for c in right.get("cases", []) if c.get("scores")}
    shared = sorted(set(lc) & set(rc), key=str)
    if not shared:
        print("겹치는 케이스가 없습니다.")
        return 1

    lv = left.get("run", {}).get("judge_prompt_version")
    rv = right.get("run", {}).get("judge_prompt_version")
    print("=" * 70)
    print(f"  루브릭 비교  {left_path.name} ({lv})  →  {right_path.name} ({rv})")
    print("=" * 70)
    print(f"  공통 케이스: {len(shared)}건")
    print("─" * 70)
    print(f"  {'차원':<20} {'이전':>8} {'이후':>8} {'차이':>9}")
    print("─" * 70)
    for dim in DIMS:
        a = [lc[i]["scores"][dim] for i in shared if lc[i]["scores"].get(dim) is not None]
        b = [rc[i]["scores"][dim] for i in shared if rc[i]["scores"].get(dim) is not None]
        if a and b:
            ma, mb = statistics.mean(a), statistics.mean(b)
            print(f"  {dim:<20} {ma:>8.3f} {mb:>8.3f} {mb - ma:>+9.3f}")
    print("─" * 70)

    changed = [
        (i, lc[i]["scores"], rc[i]["scores"])
        for i in shared
        if any(lc[i]["scores"].get(d) != rc[i]["scores"].get(d) for d in DIMS)
    ]
    print(f"  점수가 바뀐 케이스: {len(changed)}건")
    for case_id, before, after in changed:
        diffs = ", ".join(
            f"{d} {before.get(d)}→{after.get(d)}"
            for d in DIMS if before.get(d) != after.get(d)
        )
        n_products = rc[case_id].get("n_products")
        print(f"    id{case_id:>3} (n_products={n_products}): {diffs}")
    print("=" * 70)
    return 0


def main():
    ap = argparse.ArgumentParser(description="저장된 응답을 다른 루브릭으로 재채점")
    ap.add_argument("--report", help="재채점할 원본 리포트 JSON")
    ap.add_argument("--judge-prompt", default=JUDGE_PROMPT_NAME)
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--judge-base-url", default=None)
    ap.add_argument("--judge-api-key-env", default="JUDGE_API_KEY")
    ap.add_argument("--judge-timeout", type=float, default=120.0)
    ap.add_argument("--judge-repeats", type=int, default=1)
    ap.add_argument("--allow-self-judge", action="store_true")
    ap.add_argument("--bootstrap-samples", type=int, default=2_000)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE.json", "AFTER.json"),
                    help="두 채점 결과를 비교하고 종료")
    args = ap.parse_args()

    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))
    if not args.report:
        ap.error("--report 가 필요합니다 (또는 --compare 사용)")

    try:
        judge_config = build_judge_config(
            model=args.judge_model, base_url=args.judge_base_url,
            api_key_env=args.judge_api_key_env, timeout_seconds=args.judge_timeout,
            allow_self_judge=args.allow_self_judge,
        )
        report = asyncio.run(rejudge(
            Path(args.report), judge_config, args.judge_prompt,
            args.judge_repeats, args.bootstrap_samples, args.seed,
        ))
    except (ValueError, FileNotFoundError) as exc:
        ap.error(str(exc))

    out = Path(args.out) if args.out else Path(args.report).with_name(
        Path(args.report).stem + "-rejudged.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    m = report["metrics"]
    print(f"\n  OVERALL {m.get('resp_overall')} / 5   (루브릭 {report['run']['judge_prompt_version']})")
    print(f"결과 저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
