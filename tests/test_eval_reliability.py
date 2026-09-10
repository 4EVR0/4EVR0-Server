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


class _FakeConversationStore:
    """Redis 대화 이력 스텁 — 세션별 턴 보관 + clear 호출 기록."""

    def __init__(self):
        self.turns: dict[str, list] = {}
        self.cleared: list[str] = []

    async def clear(self, session_id):
        self.cleared.append(session_id)
        self.turns.pop(session_id, None)

    async def load_recent(self, session_id, limit=None):
        return list(self.turns.get(session_id, []))

    async def append_turn(self, session_id, **entry):
        self.turns.setdefault(session_id, []).append(entry)


def _write_dataset(path: Path, n: int) -> Path:
    """n개 케이스짜리 최소 데이터셋."""
    path.write_text(
        "\n".join(
            json.dumps({
                "id": i,
                "label": f"case{i}",
                "message": f"고민 {i}",
                "skin_types": [],
                "concerns": ["DULLNESS"],
                "constraints": [],
            })
            for i in range(1, n + 1)
        ),
        encoding="utf-8",
    )
    return path


def _run_response_eval(dataset, monkeypatch, store, *, session_mode):
    """recommend/judge를 스텁으로 갈아끼우고 평가를 돌린다.

    스텁 recommend는 실제 파이프라인처럼 턴을 이력에 남긴다 — 공유 세션일 때
    다음 케이스가 그 이력을 보게 되는지 확인하기 위함.
    """
    async def fake_recommend(session_id, message, _gen_prompt=None):
        await store.append_turn(session_id, user=message, assistant="응답")
        return SimpleNamespace(ingredients=[], products=[], response_text="추천 응답")

    async def fake_judge(*_args):
        return {**{dim: 4 for dim in DIMS}, "comment": "ok"}

    monkeypatch.setattr(response_eval, "conversation_store", store)
    monkeypatch.setattr(response_eval, "recommend", fake_recommend)
    monkeypatch.setattr(response_eval, "build_judge_client", lambda _config: object())
    monkeypatch.setattr(response_eval, "judge_response", fake_judge)
    config = response_eval.JudgeConfig(
        model="external/judge",
        base_url="https://judge.example/v1",
        api_key="secret",
        timeout_seconds=30,
    )
    return asyncio.run(
        response_eval.run(
            dataset,
            None,
            response_eval.DEFAULT_GEN_PROMPT,
            config,
            bootstrap_samples=100,
            seed=23,
            session_mode=session_mode,
        )
    )


def test_isolated_mode_gives_each_case_a_clean_session(tmp_path, monkeypatch):
    dataset = _write_dataset(tmp_path / "dataset.jsonl", 3)
    store = _FakeConversationStore()

    report = _run_response_eval(dataset, monkeypatch, store, session_mode="isolated")

    sessions = [row["session_id"] for row in report["cases"]]
    assert len(set(sessions)) == 3, "케이스마다 서로 다른 세션이어야 한다"
    # 어떤 케이스도 이전 대화가 남은 상태로 실행되지 않는다.
    assert all(row["history_len_before"] == 0 for row in report["cases"])
    assert report["metrics"]["contaminated_cases"] == 0
    assert report["metrics"]["contamination_rate"] == 0.0
    # 케이스가 남긴 이력을 다음으로 넘기지 않는다(실행 전/후 정리).
    assert store.turns == {}
    for session_id in sessions:
        assert store.cleared.count(session_id) == 2


def test_isolated_sessions_differ_between_runs(tmp_path, monkeypatch):
    """이력 TTL 안에 같은 평가를 다시 돌려도 실행 간 이력이 섞이지 않는다."""
    dataset = _write_dataset(tmp_path / "dataset.jsonl", 2)

    first = _run_response_eval(dataset, monkeypatch, _FakeConversationStore(), session_mode="isolated")
    second = _run_response_eval(dataset, monkeypatch, _FakeConversationStore(), session_mode="isolated")

    assert first["run"]["run_id"] != second["run"]["run_id"]
    assert not set(row["session_id"] for row in first["cases"]) & set(
        row["session_id"] for row in second["cases"]
    )


def test_shared_mode_reproduces_cross_case_contamination(tmp_path, monkeypatch):
    """격리 이전 동작 재현 — 2번째 케이스부터 앞 케이스의 이력을 본다."""
    dataset = _write_dataset(tmp_path / "dataset.jsonl", 3)
    store = _FakeConversationStore()

    report = _run_response_eval(dataset, monkeypatch, store, session_mode="shared")

    assert {row["session_id"] for row in report["cases"]} == {
        response_eval.LEGACY_SHARED_SESSION_ID
    }
    assert [row["history_len_before"] for row in report["cases"]] == [0, 1, 2]
    assert report["metrics"]["contaminated_cases"] == 2
    assert store.cleared == [], "shared 모드는 이력을 지우지 않는다"


def test_report_stores_evidence_context_for_human_labeling(tmp_path, monkeypatch):
    """사람 라벨러가 judge와 같은 근거를 보고 채점하려면 리포트에 근거가 남아야 한다."""
    from app.schemas.recommend import IngredientResult, ProductResult

    dataset = _write_dataset(tmp_path / "dataset.jsonl", 1)
    store = _FakeConversationStore()
    ingredient = IngredientResult(name="NIACINAMIDE", kor_name="나이아신아마이드",
                                  claim="피지 조절", eligibility_tier="A", paper_ref="p1")
    product = ProductResult(product_id="P1", product_name="테스트 세럼", brand="브랜드",
                            category="세럼", matched_count=1,
                            matched_ingredients=["NIACINAMIDE"])

    async def fake_recommend(session_id, message, _gen_prompt=None):
        return SimpleNamespace(ingredients=[ingredient], products=[product],
                               response_text="추천 응답")

    async def fake_judge(*_args):
        return {**{dim: 4 for dim in DIMS}, "comment": "ok"}

    monkeypatch.setattr(response_eval, "conversation_store", store)
    monkeypatch.setattr(response_eval, "recommend", fake_recommend)
    monkeypatch.setattr(response_eval, "build_judge_client", lambda _config: object())
    monkeypatch.setattr(response_eval, "judge_response", fake_judge)
    config = response_eval.JudgeConfig(model="external/judge", base_url="https://judge.example/v1",
                                       api_key="secret", timeout_seconds=30)

    report = asyncio.run(
        response_eval.run(dataset, None, response_eval.DEFAULT_GEN_PROMPT, config,
                          bootstrap_samples=50, seed=23, session_mode="isolated")
    )

    evidence = report["cases"][0]["evidence"]
    assert "NIACINAMIDE" in evidence["ingredients"]
    assert "테스트 세럼" in evidence["products"]
    # judge에게 준 것과 같은 문자열이어야 한다.
    assert evidence == response_eval.render_evidence_context([ingredient], [product])


def test_eval_default_prompt_follows_service_setting():
    """기준선은 운영이 실제로 쓰는 프롬프트를 측정해야 한다."""
    assert response_eval.DEFAULT_GEN_PROMPT == settings.gen_prompt_name


def test_run_flags_prompt_mismatch_with_service(tmp_path, monkeypatch):
    dataset = _write_dataset(tmp_path / "dataset.jsonl", 1)
    store = _FakeConversationStore()

    async def fake_recommend(session_id, message, _gen_prompt=None):
        return SimpleNamespace(ingredients=[], products=[], response_text="응답")

    async def fake_judge(*_args):
        return {**{dim: 4 for dim in DIMS}, "comment": "ok"}

    monkeypatch.setattr(response_eval, "conversation_store", store)
    monkeypatch.setattr(response_eval, "recommend", fake_recommend)
    monkeypatch.setattr(response_eval, "build_judge_client", lambda _config: object())
    monkeypatch.setattr(response_eval, "judge_response", fake_judge)
    config = response_eval.JudgeConfig(
        model="external/judge", base_url="https://judge.example/v1",
        api_key="secret", timeout_seconds=30,
    )

    def _run(gen_prompt):
        return asyncio.run(
            response_eval.run(dataset, None, gen_prompt, config,
                              bootstrap_samples=50, seed=23, session_mode="isolated")
        )

    same = _run(settings.gen_prompt_name)
    assert same["run"]["matches_service_prompt"] is True

    other = "recommend_response"  # 운영 기본이 아닌 과거 버전
    assert other != settings.gen_prompt_name
    differing = _run(other)
    assert differing["run"]["matches_service_prompt"] is False
    assert differing["run"]["service_gen_prompt"] == settings.gen_prompt_name
    assert (
        differing["run"]["service_gen_prompt_version"]
        != differing["run"]["gen_prompt_version"]
    )


def test_run_records_session_isolation_conditions(tmp_path, monkeypatch):
    dataset = _write_dataset(tmp_path / "dataset.jsonl", 1)

    report = _run_response_eval(dataset, monkeypatch, _FakeConversationStore(), session_mode="isolated")

    assert report["run"]["session_mode"] == "isolated"
    assert report["run"]["run_id"]
    assert "conversation_enabled" in report["run"]
    assert "conversation_ttl_seconds" in report["run"]


def test_run_rejects_unknown_session_mode(tmp_path, monkeypatch):
    dataset = _write_dataset(tmp_path / "dataset.jsonl", 1)

    with pytest.raises(ValueError, match="session_mode"):
        _run_response_eval(dataset, monkeypatch, _FakeConversationStore(), session_mode="nope")


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
