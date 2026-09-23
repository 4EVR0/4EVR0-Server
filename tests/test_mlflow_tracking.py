import json

import mlflow
import pytest

from eval.mlflow_tracking import (
    EXPERIMENTS,
    _report_metrics,
    log_report,
    report_kind,
)


def test_report_kind_and_nested_retrieval_metrics():
    report = {
        "run": {"timestamp": "2026-09-23T00:00:00+00:00", "judge_model": "judge"},
        "metrics": {
            "product_precision": {"mean": 0.91, "ci95": [0.85, 0.97], "n": 23},
            "error_rate": 0.0,
        },
    }
    assert report_kind(report) == "retrieval"
    assert _report_metrics(report, "retrieval") == {
        "product_precision_mean": 0.91,
        "product_precision_ci95_low": 0.85,
        "product_precision_ci95_high": 0.97,
        "product_precision_n": 23.0,
        "error_rate": 0.0,
    }


def test_unknown_report_rejected():
    with pytest.raises(ValueError, match="지원하지 않는"):
        report_kind({"metrics": {"error_rate": 0}})


def test_calibration_metrics_do_not_require_missing_historical_metadata():
    report = {
        "n_cases": 20,
        "dimensions": {"grounding": {"mae": 0.4, "spearman": None}},
        "primary": {"mae": 0.3},
        "overall": {"mae": 0.5},
    }
    assert report_kind(report) == "calibration"
    assert _report_metrics(report, "calibration") == {
        "n_cases": 20.0,
        "grounding_mae": 0.4,
        "primary_mae": 0.3,
        "overall_mae": 0.5,
    }


def test_backfill_is_idempotent_and_keeps_original_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    uri = f"sqlite:///{tmp_path / 'tracking.db'}"
    path = tmp_path / "extraction.json"
    path.write_text(json.dumps({
        "run": {
            "timestamp": "2026-09-23T00:00:00+00:00",
            "code_sha": "abc123", "dataset_sha256": "dataset123", "model": "gpu-model",
        },
        "metrics": {"concern_f1": 0.95, "latency_p50": None},
        "cases": [],
    }), encoding="utf-8")

    first, run_id = log_report(path, source="backfill", tracking_uri=uri)
    second, same_id = log_report(path, source="backfill", tracking_uri=uri)
    assert (first, second) == ("logged", "skipped")
    assert same_id == run_id

    run = mlflow.get_run(run_id)
    assert run.data.params["code_sha"] == "abc123"
    assert run.data.params["dataset_sha256"] == "dataset123"
    assert run.data.metrics["concern_f1"] == 0.95
    assert run.data.tags["provenance"] == "backfill"
    assert run.data.tags["original_timestamp"] == "2026-09-23T00:00:00+00:00"
    assert mlflow.get_experiment(run.info.experiment_id).name == EXPERIMENTS["extraction"]
    assert mlflow.get_experiment(run.info.experiment_id).artifact_location.startswith(tmp_path.as_uri())
    assert len(mlflow.MlflowClient().list_artifacts(run_id, "reports")) == 1
