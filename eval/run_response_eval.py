"""응답 품질 평가 (LLM-as-judge).

추출 평가(run_eval.py)가 못 보는 **생성된 추천문 자체의 품질**을 측정한다.
각 메시지에 대해 실제 추천 파이프라인(recommend_service.recommend)을 돌려 응답을 만들고,
그 응답을 LLM 심판(judge)이 루브릭으로 1~5점 채점한다.

전제: Neo4j(EC2) + vLLM 가동. (HTTP/세션 불필요 — 서비스 함수 직접 호출)

사용:
    JUDGE_MODEL=<external-model> JUDGE_API_KEY=<key> python eval/run_response_eval.py
    python eval/run_response_eval.py --judge-model <external-model> --limit 5

기본 동작은 생성기와 다른 외부 judge를 요구하고, 생성 temperature를 0으로 고정한다.
self-judge는 편향을 명시적으로 감수하는 --allow-self-judge 없이는 실행되지 않는다.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import openai

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.prompts import load_prompt, prompt_version  # noqa: E402
from app.services.recommend_service import (  # noqa: E402
    _evidence_label,
    _ingredient_display_name,
    recommend,
)
from eval.eval_utils import (  # noqa: E402
    bootstrap_mean_ci,
    file_sha256,
    load_dataset,
    pearson_correlation,
    spearman_correlation,
)

JUDGE_PROMPT_NAME = "response_judge"
DEFAULT_GEN_PROMPT = "recommend_response"  # 평가 대상 응답 생성 프롬프트(--gen-prompt로 교체)
DIMS = ["concern_fit", "grounding", "conciseness", "korean_quality", "format_adherence"]
DEFAULT_JUDGE_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float


def _normalize_base_url(url: str) -> str:
    value = url.strip().rstrip("/")
    if not value:
        raise ValueError("judge base URL must not be empty")
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def build_judge_config(
    *,
    model: str | None,
    base_url: str | None,
    api_key_env: str,
    timeout_seconds: float,
    allow_self_judge: bool,
) -> JudgeConfig:
    judge_model = (model or os.environ.get("JUDGE_MODEL", "")).strip()
    if not judge_model:
        raise ValueError("external judge model is required: set JUDGE_MODEL or --judge-model")

    judge_base_url = _normalize_base_url(
        base_url or os.environ.get("JUDGE_BASE_URL", DEFAULT_JUDGE_BASE_URL)
    )
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise ValueError(f"judge API key is required: set {api_key_env}")

    same_model = judge_model == settings.gpu_model
    if same_model and not allow_self_judge:
        raise ValueError(
            "judge resolves to the generator model; configure a different external model "
            "or pass --allow-self-judge to acknowledge bias"
        )
    return JudgeConfig(judge_model, judge_base_url, api_key, timeout_seconds)


def build_judge_client(config: JudgeConfig) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout_seconds,
    )


async def judge_response(client, model, message, ingredients, products, response, judge_prompt) -> dict:
    """응답을 심판 LLM에게 채점받아 dict 반환.

    심판에게 생성기와 '동일한' 근거 컨텍스트(근거 수준·제품 핵심성분)를 줘야 grounding을
    공정하게 채점한다. 안 주면 응답의 '논문 근거 N건' 인용을 검증 못 해 hallucination으로 오판한다.
    """
    # ev/_annotate 매핑은 제품 핵심성분(영문 inci)과 키를 맞춰야 하므로 i.name(영문) 유지.
    # 성분 라인 표기는 generator와 동일하게 '한글명(영어명)' 형태로 맞춰 공정 채점.
    ev = {i.name: _evidence_label(i.eligibility_tier, i.paper_ref) for i in ingredients}
    ing_lines = "\n".join(
        f"- {_ingredient_display_name(i)}: {i.claim or '효능 데이터 없음'} ({_evidence_label(i.eligibility_tier, i.paper_ref)})"
        for i in ingredients[:10]
    ) or "(없음)"

    def _annotate(names: list[str]) -> str:
        return ", ".join(f"{n}({ev.get(n, '근거 미상')})" for n in names[:3])

    prod_lines = "\n".join(
        f"- [{p.category}] {p.brand} {p.product_name} (핵심성분: {_annotate(p.matched_ingredients)})"
        for p in products
    ) or "(없음)"
    content = (
        f"[User message]\n{message}\n\n"
        f"[Provided ingredients]\n{ing_lines}\n\n"
        f"[Provided products]\n{prod_lines}\n\n"
        f"[Assistant response]\n{response}"
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": judge_prompt},
            {"role": "user", "content": content},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    # 1~5 범위로 클램프 + 누락 방지
    scores = {}
    for d in DIMS:
        try:
            scores[d] = max(1, min(5, int(round(float(data.get(d))))))
        except (TypeError, ValueError):
            scores[d] = None
    missing_scores = [dim for dim in DIMS if scores[dim] is None]
    if missing_scores:
        raise ValueError(f"judge response is missing numeric scores: {missing_scores}")
    scores["comment"] = str(data.get("comment", ""))[:200]
    return scores


def load_human_scores(path: Path) -> dict[int | str, dict[str, float]]:
    rows: dict[int | str, dict[str, float]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row.get("id")
        if case_id is None or case_id in rows:
            raise ValueError(f"{path}:{line_number}: missing or duplicate id")
        raw_scores = row.get("scores", {})
        scores: dict[str, float] = {}
        for dim in DIMS:
            value = raw_scores.get(dim)
            if not isinstance(value, (int, float)) or not 1 <= float(value) <= 5:
                raise ValueError(f"{path}:{line_number}: {dim} must be a number from 1 to 5")
            scores[dim] = float(value)
        rows[case_id] = scores
    if not rows:
        raise ValueError(f"{path}: human label file is empty")
    return rows


def calibrate_against_humans(results: list[dict], human_scores: dict) -> dict:
    judged = {row["id"]: row["scores"] for row in results if "scores" in row}
    shared_ids = sorted(set(judged) & set(human_scores), key=str)
    if not shared_ids:
        raise ValueError("human labels do not overlap with successfully judged case IDs")
    dimensions: dict[str, dict] = {}
    all_judge: list[float] = []
    all_human: list[float] = []
    for dim in DIMS:
        judge_values = [float(judged[case_id][dim]) for case_id in shared_ids]
        human_values = [float(human_scores[case_id][dim]) for case_id in shared_ids]
        all_judge.extend(judge_values)
        all_human.extend(human_values)
        dimensions[dim] = {
            "mae": round(statistics.mean(abs(a - b) for a, b in zip(judge_values, human_values)), 4)
            if judge_values else None,
            "pearson": pearson_correlation(judge_values, human_values),
            "spearman": spearman_correlation(judge_values, human_values),
        }
    return {
        "n_cases": len(shared_ids),
        "case_ids": shared_ids,
        "dimensions": dimensions,
        "overall": {
            "mae": round(statistics.mean(abs(a - b) for a, b in zip(all_judge, all_human)), 4)
            if all_judge else None,
            "pearson": pearson_correlation(all_judge, all_human),
            "spearman": spearman_correlation(all_judge, all_human),
        },
    }


async def run(
    dataset_path: Path,
    limit: int | None,
    gen_prompt: str,
    judge_config: JudgeConfig,
    *,
    judge_repeats: int = 1,
    gen_temperature: float = 0.0,
    bootstrap_samples: int = 2_000,
    seed: int = 23,
    human_labels_path: Path | None = None,
) -> dict:
    cases = load_dataset(dataset_path)
    if limit:
        cases = cases[:limit]
    if judge_repeats < 1:
        raise ValueError("judge_repeats must be at least 1")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be at least 1")
    settings.gen_temperature = gen_temperature
    client = build_judge_client(judge_config)
    judge_prompt = load_prompt(JUDGE_PROMPT_NAME)

    results = []
    dim_scores = {d: [] for d in DIMS}
    errors = 0
    gen_latencies = []
    repeat_stddevs: list[float] = []

    for case in cases:
        row = {"id": case["id"], "label": case.get("label", ""), "message": case["message"]}
        try:
            t0 = time.perf_counter()
            rec = await recommend("eval-response", case["message"], gen_prompt)  # 실제 파이프라인 (Neo4j+vLLM)
            gen_latencies.append(time.perf_counter() - t0)
            score_runs = [
                await judge_response(
                    client,
                    judge_config.model,
                    case["message"],
                    rec.ingredients,
                    rec.products,
                    rec.response_text,
                    judge_prompt,
                )
                for _ in range(judge_repeats)
            ]
            scores = {
                dim: round(statistics.mean(run[dim] for run in score_runs if run[dim] is not None), 3)
                if any(run[dim] is not None for run in score_runs) else None
                for dim in DIMS
            }
            scores["comment"] = score_runs[0]["comment"]
            if judge_repeats > 1:
                repeat_stddevs.extend(
                    statistics.pstdev([run[dim] for run in score_runs if run[dim] is not None])
                    for dim in DIMS
                    if any(run[dim] is not None for run in score_runs)
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
    for index, dim in enumerate(DIMS):
        ci = bootstrap_mean_ci(dim_scores[dim], samples=bootstrap_samples, seed=seed + index)
        if ci:
            metrics[f"resp_{dim}_ci95_low"], metrics[f"resp_{dim}_ci95_high"] = ci
    all_means = [statistics.mean(dim_scores[d]) for d in DIMS if dim_scores[d]]
    metrics["resp_overall"] = round(statistics.mean(all_means), 3) if all_means else None
    case_overalls = [
        row["overall"] for row in results if row.get("overall") is not None
    ]
    overall_ci = bootstrap_mean_ci(case_overalls, samples=bootstrap_samples, seed=seed)
    if overall_ci:
        metrics["resp_overall_ci95_low"], metrics["resp_overall_ci95_high"] = overall_ci
    metrics["error_rate"] = round(errors / len(cases), 4) if cases else 0.0
    metrics["gen_latency_p50"] = round(statistics.median(gen_latencies), 2) if gen_latencies else None
    metrics["judge_repeat_stddev"] = (
        round(statistics.mean(repeat_stddevs), 4) if repeat_stddevs else 0.0
    )

    run_info = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "generator_model": settings.gpu_model,
        "generator_base_url": _normalize_base_url(settings.gpu_server_url),
        "generator_temperature": gen_temperature,
        "gen_prompt": gen_prompt,
        "gen_prompt_version": prompt_version(gen_prompt),
        "judge_model": judge_config.model,
        "judge_base_url": judge_config.base_url,
        "judge_temperature": 0,
        "judge_repeats": judge_repeats,
        "judge_prompt_version": prompt_version(JUDGE_PROMPT_NAME),
        "dataset": str(dataset_path),
        "dataset_sha256": file_sha256(dataset_path),
        "n_cases": len(cases),
        "n_scored": scored,
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_seed": seed,
    }
    report = {"run": run_info, "metrics": metrics, "cases": results}
    if human_labels_path:
        report["human_calibration"] = calibrate_against_humans(
            results, load_human_scores(human_labels_path)
        )
    return report


def print_summary(report: dict) -> None:
    r, m = report["run"], report["metrics"]
    print("\n" + "═" * 60)
    print("  응답 품질 평가 (LLM-judge)")
    print("═" * 60)
    print(
        f"  generator={r['generator_model']}  judge={r['judge_model']}  "
        f"gen_prompt={r['gen_prompt_version']}  judge_prompt={r['judge_prompt_version']}"
    )
    print(f"  n={r['n_cases']} (scored {r['n_scored']})")
    print("─" * 60)
    for d in DIMS:
        if f"resp_{d}" in m:
            low = m.get(f"resp_{d}_ci95_low")
            high = m.get(f"resp_{d}_ci95_high")
            ci_text = f" (95% CI {low:.2f}–{high:.2f})" if low is not None and high is not None else ""
            print(f"  {d:<20} {m['resp_' + d]:.2f} / 5{ci_text}")
    print("─" * 60)
    overall_ci = (
        f" (95% CI {m['resp_overall_ci95_low']:.2f}–{m['resp_overall_ci95_high']:.2f})"
        if "resp_overall_ci95_low" in m else ""
    )
    print(f"  {'OVERALL':<20} {m.get('resp_overall')} / 5{overall_ci}")
    print(f"  {'에러율':<20} {m['error_rate']}")
    print(f"  {'응답생성 p50':<20} {m.get('gen_latency_p50')}s")
    print(f"  {'judge 반복 표준편차':<20} {m.get('judge_repeat_stddev')}")
    if "human_calibration" in report:
        calibration = report["human_calibration"]
        print(
            f"  {'human 보정':<20} n={calibration['n_cases']} "
            f"MAE={calibration['overall']['mae']} "
            f"Spearman={calibration['overall']['spearman']}"
        )
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
        parameter_names = (
            "generator_model", "generator_base_url", "generator_temperature",
            "gen_prompt", "gen_prompt_version", "judge_model", "judge_base_url",
            "judge_temperature", "judge_repeats", "judge_prompt_version",
            "dataset_sha256", "n_cases", "n_scored", "bootstrap_samples", "bootstrap_seed",
        )
        mlflow.log_params({key: run[key] for key in parameter_names})
        mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
        if "human_calibration" in report:
            calibration = report["human_calibration"]
            mlflow.log_metrics({
                f"human_{key}": value
                for key, value in calibration["overall"].items()
                if isinstance(value, (int, float))
            })
            mlflow.log_param("human_n_cases", calibration["n_cases"])
        if artifact_path:
            mlflow.log_artifact(str(artifact_path))
    print(f"  MLflow 기록: experiment='4evr0-response-quality' @ {tracking_uri}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(_REPO_ROOT / "eval" / "dataset.jsonl"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--gen-prompt", default=DEFAULT_GEN_PROMPT,
                    help="응답 생성 프롬프트 이름 (예: recommend_response.v2)")
    ap.add_argument("--gen-temperature", type=float, default=0.0,
                    help="재현성을 위해 기본 0.0 (운영값 재현 시 명시적으로 변경)")
    ap.add_argument("--judge-model", default=None,
                    help="외부 judge 모델 (기본: JUDGE_MODEL)")
    ap.add_argument("--judge-base-url", default=None,
                    help=f"OpenAI 호환 judge URL (기본: JUDGE_BASE_URL 또는 {DEFAULT_JUDGE_BASE_URL})")
    ap.add_argument("--judge-api-key-env", default="JUDGE_API_KEY",
                    help="judge API 키를 읽을 환경변수 이름")
    ap.add_argument("--judge-timeout", type=float, default=120.0)
    ap.add_argument("--judge-repeats", type=int, default=1,
                    help="동일 응답 반복 채점 횟수; 분산 확인 시 3 이상 권장")
    ap.add_argument("--allow-self-judge", action="store_true",
                    help="생성기와 동일한 judge 사용을 명시적으로 허용")
    ap.add_argument("--human-labels", default=None,
                    help="전문가 점수 JSONL; judge-vs-human MAE/상관 산출")
    ap.add_argument("--bootstrap-samples", type=int, default=2_000)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-mlflow", action="store_true")
    args = ap.parse_args()

    try:
        judge_config = build_judge_config(
            model=args.judge_model,
            base_url=args.judge_base_url,
            api_key_env=args.judge_api_key_env,
            timeout_seconds=args.judge_timeout,
            allow_self_judge=args.allow_self_judge,
        )
        report = asyncio.run(
            run(
                Path(args.dataset),
                args.limit,
                args.gen_prompt,
                judge_config,
                judge_repeats=args.judge_repeats,
                gen_temperature=args.gen_temperature,
                bootstrap_samples=args.bootstrap_samples,
                seed=args.seed,
                human_labels_path=Path(args.human_labels) if args.human_labels else None,
            )
        )
    except ValueError as exc:
        ap.error(str(exc))
    print_summary(report)

    out = Path(args.out) if args.out else _REPO_ROOT / "eval" / "results" / f"resp-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")

    if not args.no_mlflow:
        log_to_mlflow(report, out)


if __name__ == "__main__":
    main()
