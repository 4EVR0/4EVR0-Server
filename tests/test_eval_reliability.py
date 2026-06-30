import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.domain.enums import Concern, Constraint, SkinType
import eval.run_response_eval as response_eval
from eval.eval_utils import (
    bootstrap_mean_ci,
    load_dataset,
    pearson_correlation,
    spearman_correlation,
)
from eval.run_response_eval import (
    DIMS,
    build_judge_config,
    calibrate_against_humans,
    load_human_scores,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shared_dataset_has_50_valid_unique_cases():
    cases = load_dataset(REPO_ROOT / "eval" / "dataset.jsonl")

    assert len(cases) == 50
    assert len({case["id"] for case in cases}) == 50
    assert {value for case in cases for value in case["skin_types"]} == {item.value for item in SkinType}
    assert {value for case in cases for value in case["concerns"]} == {item.value for item in Concern}
    assert {value for case in cases for value in case["constraints"]} == {item.value for item in Constraint}


def test_dataset_validation_rejects_unknown_enum(tmp_path):
    dataset = tmp_path / "invalid.jsonl"
    dataset.write_text(
        json.dumps({
            "id": 1,
            "message": "test",
            "skin_types": [],
            "concerns": ["NOT_A_CONCERN"],
            "constraints": [],
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid concerns"):
        load_dataset(dataset)


def test_bootstrap_ci_is_reproducible():
    first = bootstrap_mean_ci([1, 2, 3, 4, 5], samples=500, seed=23)
    second = bootstrap_mean_ci([1, 2, 3, 4, 5], samples=500, seed=23)

    assert first == second
    assert first is not None
    assert first[0] <= 3 <= first[1]


def test_correlations_support_ties_and_perfect_order():
    assert pearson_correlation([1, 2, 3], [2, 4, 6]) == 1.0
    assert spearman_correlation([1, 2, 2, 4], [1, 3, 3, 5]) == 1.0
    assert pearson_correlation([1, 1], [2, 3]) is None


def test_judge_config_rejects_same_model_and_endpoint(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "EMPTY")

    with pytest.raises(ValueError, match="generator model"):
        build_judge_config(
            model=settings.gpu_model,
            base_url=settings.gpu_server_url,
            api_key_env="TEST_JUDGE_KEY",
            timeout_seconds=30,
            allow_self_judge=False,
        )


def test_judge_config_accepts_external_model(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "secret")

    config = build_judge_config(
        model="external/judge-model",
        base_url="https://judge.example/api",
        api_key_env="TEST_JUDGE_KEY",
        timeout_seconds=30,
        allow_self_judge=False,
    )

    assert config.model == "external/judge-model"
    assert config.base_url == "https://judge.example/api/v1"
    assert config.api_key == "secret"


def test_human_calibration_reports_agreement(tmp_path):
    human_path = tmp_path / "human.jsonl"
    human_rows = [
        {"id": 1, "scores": {dim: 2 for dim in DIMS}},
        {"id": 2, "scores": {dim: 4 for dim in DIMS}},
    ]
    human_path.write_text(
        "\n".join(json.dumps(row) for row in human_rows),
        encoding="utf-8",
    )
    judged = [
        {"id": 1, "scores": {dim: 2 for dim in DIMS}},
        {"id": 2, "scores": {dim: 4 for dim in DIMS}},
    ]

    calibration = calibrate_against_humans(judged, load_human_scores(human_path))

    assert calibration["n_cases"] == 2
    assert calibration["overall"] == {"mae": 0.0, "pearson": 1.0, "spearman": 1.0}


def test_response_run_records_reproducibility_metadata(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps({
            "id": 1,
            "label": "test",
            "message": "칙칙해요",
            "skin_types": [],
            "concerns": ["DULLNESS"],
            "constraints": [],
        }),
        encoding="utf-8",
    )

    async def fake_recommend(*_args):
        return SimpleNamespace(
            ingredients=[],
            products=[],
            response_text="추천 응답",
        )

    async def fake_judge(*_args):
        return {**{dim: 4 for dim in DIMS}, "comment": "ok"}

    monkeypatch.setattr(response_eval, "recommend", fake_recommend)
    monkeypatch.setattr(response_eval, "build_judge_client", lambda _config: object())
    monkeypatch.setattr(response_eval, "judge_response", fake_judge)
    config = response_eval.JudgeConfig(
        model="external/judge",
        base_url="https://judge.example/v1",
        api_key="secret",
        timeout_seconds=30,
    )

    report = asyncio.run(
        response_eval.run(
            dataset,
            None,
            response_eval.DEFAULT_GEN_PROMPT,
            config,
            judge_repeats=2,
            bootstrap_samples=100,
            seed=23,
        )
    )

    assert report["metrics"]["resp_overall"] == 4
    assert report["metrics"]["resp_overall_ci95_low"] == 4
    assert report["run"]["judge_model"] == "external/judge"
    assert report["run"]["generator_temperature"] == 0
    assert report["run"]["dataset_sha256"]
