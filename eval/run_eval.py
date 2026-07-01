"""오프라인 평가 러너 — 프로필 추출 품질을 정답셋으로 측정한다.

`eval/dataset.jsonl` 의 각 메시지에 대해 LLM 프로필 추출을 실행하고,
기대 라벨과 비교해 정확도/무효값/폴백/지연/토큰을 산출한다.

사용:
    # vLLM(GPU_SERVER_URL) 가동 + .env 모델명 일치 필요
    python eval/run_eval.py                 # 전체
    python eval/run_eval.py --limit 5       # 앞 5개만
    python eval/run_eval.py --out eval/results/myrun.json

결과는 콘솔 요약 + JSON(run 파라미터 + 집계 metrics + 케이스별 상세)으로 저장한다.
이 run/metrics 딕셔너리는 다음 단계에서 MLflow(log_params/log_metrics)로 그대로 넘기기 쉽게 구성했다.
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

from app.clients.llm_client import build_extract_extra_body  # noqa: E402
from app.clients.llm_factory import get_async_llm_client  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.domain.enums import Concern, Constraint, SkinType  # noqa: E402
from app.prompts import load_prompt, prompt_version  # noqa: E402
from eval.eval_utils import load_dataset  # noqa: E402

_VALID = {
    "skin_types": {e.value for e in SkinType},
    "concerns": {e.value for e in Concern},
    "constraints": {e.value for e in Constraint},
}
# 추출 프롬프트 기본값. --prompt 로 교체 가능(예: profile_extraction.v2).
# 프롬프트 내용이 바뀌면 prompt_version(해시)도 바뀜 → MLflow에서 실험 비교 시 추적.
DEFAULT_PROMPT_NAME = "profile_extraction"


async def extract(client, message: str, system_prompt: str):
    """LLM 추출 1회. (raw_dict, usage, latency_s) 반환. 실패 시 예외 전파."""
    start = time.perf_counter()
    resp = await client.chat.completions.create(
        model=settings.gpu_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        extra_body=build_extract_extra_body(),
    )
    latency = time.perf_counter() - start
    raw = json.loads(resp.choices[0].message.content or "{}")
    usage = getattr(resp, "usage", None)
    tokens = {
        "prompt": getattr(usage, "prompt_tokens", 0) or 0,
        "completion": getattr(usage, "completion_tokens", 0) or 0,
    }
    return raw, tokens, latency


def split_valid(field: str, values) -> tuple[list[str], list[str]]:
    """raw 값들을 (유효, 무효=오타/없는값) 으로 분리."""
    valid_set = _VALID[field]
    values = values if isinstance(values, list) else []
    valid = [v for v in values if v in valid_set]
    invalid = [v for v in values if v not in valid_set]
    return valid, invalid


def prf_counts(pred: list[str], gold: list[str]) -> tuple[int, int, int]:
    p, g = set(pred), set(gold)
    return len(p & g), len(p - g), len(g - p)


def f1(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) else (1.0 if fn == 0 else 0.0)
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f, 4)}


async def run(dataset_path: Path, limit: int | None, prompt_name: str = DEFAULT_PROMPT_NAME) -> dict:
    cases = load_dataset(dataset_path)
    if limit:
        cases = cases[:limit]
    client = get_async_llm_client()
    system_prompt = load_prompt(prompt_name)

    results = []
    agg = {f: [0, 0, 0] for f in ("skin_types", "concerns", "constraints")}  # tp,fp,fn
    skin_exact = concern_exact = profile_exact = 0
    invalid_cases = 0
    errors = 0
    latencies, prompt_toks, completion_toks = [], [], []

    for case in cases:
        gold = {f: case[f] for f in ("skin_types", "concerns", "constraints")}
        row = {"id": case["id"], "label": case.get("label", ""), "message": case["message"], "gold": gold}
        try:
            raw, tokens, latency = await extract(client, case["message"], system_prompt)
        except Exception as exc:  # vLLM 다운/타임아웃/파싱 실패 = 운영상 폴백 상황
            errors += 1
            row["error"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
            print(f"  [id {case['id']:>2}] ERROR {type(exc).__name__}")
            continue

        pred, invalid = {}, {}
        for fld in ("skin_types", "concerns", "constraints"):
            pred[fld], invalid[fld] = split_valid(fld, raw.get(fld, []))

        any_invalid = any(invalid.values())
        invalid_cases += 1 if any_invalid else 0
        latencies.append(latency)
        prompt_toks.append(tokens["prompt"])
        completion_toks.append(tokens["completion"])

        for fld in ("skin_types", "concerns", "constraints"):
            tp, fp, fn = prf_counts(pred[fld], gold[fld])
            agg[fld][0] += tp; agg[fld][1] += fp; agg[fld][2] += fn

        st_ok = set(pred["skin_types"]) == set(gold["skin_types"])
        co_ok = set(pred["concerns"]) == set(gold["concerns"])
        cn_ok = set(pred["constraints"]) == set(gold["constraints"])
        skin_exact += st_ok
        concern_exact += co_ok
        profile_exact += st_ok and co_ok and cn_ok

        row.update({"pred": pred, "invalid": {k: v for k, v in invalid.items() if v},
                    "latency_s": round(latency, 3), "tokens": tokens,
                    "match": {"skin_types": st_ok, "concerns": co_ok, "constraints": cn_ok}})
        results.append(row)
        flag = "⚠INVALID" if any_invalid else ("✓" if co_ok else "✗concern")
        print(f"  [id {case['id']:>2}] {latency:5.2f}s  concern_match={co_ok!s:<5} {flag}")

    n = len(cases)
    scored = n - errors
    metrics = {
        "concern_f1": f1(*agg["concerns"])["f1"],
        "concern_precision": f1(*agg["concerns"])["precision"],
        "concern_recall": f1(*agg["concerns"])["recall"],
        "concern_exact_match": round(concern_exact / scored, 4) if scored else 0.0,
        "skin_type_accuracy": round(skin_exact / scored, 4) if scored else 0.0,
        "constraint_f1": f1(*agg["constraints"])["f1"],
        "profile_exact_match": round(profile_exact / scored, 4) if scored else 0.0,
        "invalid_value_rate": round(invalid_cases / scored, 4) if scored else 0.0,
        "error_rate": round(errors / n, 4) if n else 0.0,
        "latency_p50": round(statistics.median(latencies), 3) if latencies else None,
        "latency_p90": round(statistics.quantiles(latencies, n=10)[8], 3) if len(latencies) >= 2 else (round(latencies[0], 3) if latencies else None),
        "avg_prompt_tokens": round(statistics.mean(prompt_toks), 1) if prompt_toks else None,
        "avg_completion_tokens": round(statistics.mean(completion_toks), 1) if completion_toks else None,
    }
    run_info = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": settings.gpu_model,
        "prompt_name": prompt_name,
        "prompt_version": prompt_version(prompt_name),
        "temperature": 0,
        "dataset": str(dataset_path),
        "n_cases": n,
        "n_scored": scored,
    }
    return {"run": run_info, "metrics": metrics, "cases": results}


def print_summary(report: dict) -> None:
    r, m = report["run"], report["metrics"]
    print("\n" + "═" * 60)
    print("  오프라인 평가 결과")
    print("═" * 60)
    print(f"  model={r['model']}  prompt={r['prompt_version']}  n={r['n_cases']}(scored {r['n_scored']})")
    print("─" * 60)
    rows = [
        ("concern F1", m["concern_f1"]), ("  precision / recall", f"{m['concern_precision']} / {m['concern_recall']}"),
        ("concern 완전일치율", m["concern_exact_match"]), ("skin_type 정확도", m["skin_type_accuracy"]),
        ("constraint F1", m["constraint_f1"]), ("profile 완전일치율", m["profile_exact_match"]),
        ("무효값(오타) 비율", m["invalid_value_rate"]), ("폴백/에러 비율", m["error_rate"]),
        ("latency p50 / p90", f"{m['latency_p50']} / {m['latency_p90']}s"),
        ("평균 토큰(in/out)", f"{m['avg_prompt_tokens']} / {m['avg_completion_tokens']}"),
    ]
    for k, v in rows:
        print(f"  {k:<22} {v}")
    print("═" * 60)


def log_to_mlflow(report: dict, artifact_path: Path | None) -> None:
    """run/metrics 를 MLflow 에 기록. mlflow 미설치 시 조용히 건너뜀.

    추적 백엔드: 기본 sqlite(eval/mlflow.db). MLFLOW_TRACKING_URI 로 덮어쓸 수 있음.
    조회: mlflow ui --backend-store-uri sqlite:///eval/mlflow.db
    """
    try:
        import mlflow
    except ImportError:
        print("  (mlflow 미설치 — MLflow 기록 건너뜀. `pip install mlflow`)")
        return

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{_REPO_ROOT / 'eval' / 'mlflow.db'}")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("4evr0-profile-extraction")

    run, metrics = report["run"], report["metrics"]
    with mlflow.start_run(run_name=run["timestamp"]):
        mlflow.log_params({
            k: run[k] for k in ("model", "prompt_name", "prompt_version", "temperature", "dataset", "n_cases", "n_scored")
        })
        mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
        if artifact_path:
            mlflow.log_artifact(str(artifact_path))
    print(f"  MLflow 기록 완료: experiment='4evr0-profile-extraction' @ {tracking_uri}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(_REPO_ROOT / "eval" / "dataset.jsonl"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT_NAME,
                    help="추출 프롬프트 이름 (app/prompts/<name>.txt). 예: profile_extraction.v2")
    ap.add_argument("--out", default=None, help="결과 JSON 경로 (기본: eval/results/<timestamp>.json)")
    ap.add_argument("--no-mlflow", action="store_true", help="MLflow 기록 비활성화")
    args = ap.parse_args()

    report = asyncio.run(run(Path(args.dataset), args.limit, args.prompt))
    print_summary(report)

    out = Path(args.out) if args.out else _REPO_ROOT / "eval" / "results" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")

    if not args.no_mlflow:
        log_to_mlflow(report, out)


if __name__ == "__main__":
    main()
