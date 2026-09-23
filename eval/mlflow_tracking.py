"""Persist saved evaluation reports in a shared, local-by-default MLflow store.

The JSON report remains the source of truth. Importing one never calls a model.
"""

import argparse
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS = {
    "extraction": "4evr0-profile-extraction",
    "response": "4evr0-response-quality",
    "retrieval": "4evr0-retrieval-quality",
    "calibration": "4evr0-judge-calibration",
}


def default_tracking_uri() -> str:
    """Resolve the original checkout so worktrees share one ignored SQLite DB."""
    try:
        common = Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=_REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip())
        root = common.parent if common.name == ".git" else _REPO_ROOT
    except (OSError, subprocess.CalledProcessError):
        root = _REPO_ROOT
    return f"sqlite:///{root / 'eval' / 'mlflow.db'}"


def _local_artifact_location(uri: str, experiment_name: str) -> str | None:
    if not uri.startswith("sqlite:///"):
        return None
    db_path = Path(uri.removeprefix("sqlite:///"))
    if not db_path.is_absolute():
        return None
    root = db_path.parent.parent if db_path.name == "mlflow.db" and db_path.parent.name == "eval" else db_path.parent
    return (root / "mlruns" / experiment_name).as_uri()


def report_kind(report: dict) -> str:
    if "run" in report and "metrics" in report:
        run, metrics = report["run"], report["metrics"]
        if "generator_model" in run or "resp_overall" in metrics:
            return "response"
        if "product_precision" in metrics:
            return "retrieval"
        if "concern_f1" in metrics:
            return "extraction"
    if "dimensions" in report and "primary" in report and "n_cases" in report:
        return "calibration"
    raise ValueError("지원하지 않는 평가 리포트 형식입니다")


def _numeric_metrics(value: dict, prefix: str = "") -> dict[str, float]:
    result = {}
    for key, item in value.items():
        name = f"{prefix}{key}"
        if isinstance(item, dict):
            result.update(_numeric_metrics(item, f"{name}_"))
        elif key == "ci95" and isinstance(item, list) and len(item) == 2:
            for suffix, number in zip(("low", "high"), item):
                if isinstance(number, (int, float)) and math.isfinite(number):
                    result[f"{prefix}ci95_{suffix}"] = float(number)
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
            result[name] = float(item)
    return result


def _report_metrics(report: dict, kind: str) -> dict[str, float]:
    if kind == "calibration":
        metrics = {"n_cases": float(report["n_cases"])}
        for section in ("primary", "overall"):
            metrics.update(_numeric_metrics(report.get(section, {}), f"{section}_"))
        for dimension, values in report.get("dimensions", {}).items():
            metrics.update(_numeric_metrics(values, f"{dimension}_"))
        return metrics
    metrics = _numeric_metrics(report["metrics"])
    calibration = report.get("human_calibration")
    if isinstance(calibration, dict):
        metrics.update(_numeric_metrics(calibration.get("overall", {}), "human_"))
        if isinstance(calibration.get("n_cases"), int):
            metrics["human_n_cases"] = float(calibration["n_cases"])
    return metrics


def log_report(path: Path, *, source: str = "live", tracking_uri: str | None = None) -> tuple[str, str]:
    """Log one report, returning (status, run_id). Repeated imports are skipped."""
    if source not in ("live", "backfill"):
        raise ValueError("source must be live or backfill")
    raw = path.read_bytes()
    report = json.loads(raw)
    kind = report_kind(report)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        import mlflow
        from mlflow import MlflowClient
    except ImportError as exc:
        raise RuntimeError("MLflow 기록 불가: pip install -r eval/requirements.txt 필요") from exc

    uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI") or default_tracking_uri()
    mlflow.set_tracking_uri(uri)
    client = MlflowClient()
    experiment = client.get_experiment_by_name(EXPERIMENTS[kind])
    if experiment is None:
        experiment_id = client.create_experiment(
            EXPERIMENTS[kind], artifact_location=_local_artifact_location(uri, EXPERIMENTS[kind]),
        )
    else:
        experiment_id = experiment.experiment_id
    existing = client.search_runs(
        [experiment_id], filter_string=f"tags.report_sha256 = '{digest}'", max_results=1,
    )
    if existing:
        return "skipped", existing[0].info.run_id

    run_info = report.get("run", {})
    params = {
        key: value for key, value in run_info.items()
        if key != "timestamp" and isinstance(value, (str, int, float, bool))
    }
    tags = {
        "report_sha256": digest,
        "report_kind": kind,
        "provenance": source,
        "report_filename": path.name,
    }
    if run_info.get("timestamp"):
        tags["original_timestamp"] = run_info["timestamp"]
    with mlflow.start_run(experiment_id=experiment_id, run_name=run_info.get("timestamp", path.stem), tags=tags) as active:
        if params:
            mlflow.log_params(params)
        metrics = _report_metrics(report, kind)
        if metrics:
            mlflow.log_metrics(metrics)
        mlflow.log_artifact(str(path), artifact_path="reports")
        return "logged", active.info.run_id


def main() -> None:
    ap = argparse.ArgumentParser(description="저장된 평가 JSON을 MLflow에 소급 등록 (모델 호출 없음)")
    ap.add_argument("reports", nargs="+", type=Path, help="평가 JSON 경로 (여러 개 가능)")
    ap.add_argument("--tracking-uri", help="기본: MLFLOW_TRACKING_URI 또는 원본 checkout의 eval/mlflow.db")
    args = ap.parse_args()
    for path in args.reports:
        status, run_id = log_report(path, source="backfill", tracking_uri=args.tracking_uri)
        print(f"{status}: {path} (MLflow run {run_id})")


if __name__ == "__main__":
    main()
