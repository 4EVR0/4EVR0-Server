from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from eval import run_multiturn_eval as multiturn_eval
from eval.run_multiturn_eval import compare_transports, evaluate_turn, load_scenarios, parse_sse_frame


def _result(product_ids, response="정상 응답"):
    return {
        "products": [{"product_id": product_id} for product_id in product_ids],
        "ingredients": [],
        "response_text": response,
    }


def test_multiturn_dataset_is_valid_and_has_planned_coverage():
    scenarios = load_scenarios(Path("eval/multiturn_dataset.jsonl"))
    assert len(scenarios) == 15
    kinds = {turn["kind"] for scenario in scenarios for turn in scenario["turns"]}
    assert kinds == {"new", "followup", "missing_history"}


def test_followup_requires_same_product_set_but_allows_reordering():
    previous = _result(["a", "b"])
    current = _result(["b", "a"])
    assert evaluate_turn({"kind": "followup"}, current, previous) == []


def test_followup_flags_product_set_change():
    failures = evaluate_turn({"kind": "followup"}, _result(["b"]), _result(["a", "b"]))
    assert "FOLLOWUP_PRODUCT_SET_CHANGED" in failures


def test_followup_after_safe_zero_product_response_is_allowed():
    previous = _result([], "조건에 맞는 제품이 없습니다.")
    current = _result([], "이전 추천에서 조건에 맞는 제품을 찾지 못해 비교할 제품이 없습니다.")

    assert evaluate_turn({"kind": "followup"}, current, previous) == []


def test_followup_after_zero_products_must_not_invent_an_answer():
    previous = _result([], "조건에 맞는 제품이 없습니다.")
    current = _result([], "아르토닌 성분을 추천합니다.")

    assert "NO_PRODUCT_FOLLOWUP_MESSAGE_ABSENT" in evaluate_turn(
        {"kind": "followup"}, current, previous
    )


def test_missing_history_contract():
    ok = _result([], "이전 추천 내역을 찾지 못했어요. 다시 알려주세요.")
    assert evaluate_turn({"kind": "missing_history"}, ok, None) == []


def test_hanja_leak_is_deterministic_failure():
    failures = evaluate_turn({"kind": "new"}, _result([], "피肤 응답"), None)
    assert "HANJA_LEAK" in failures


def test_parse_sse_frame():
    event, data = parse_sse_frame('event: delta\ndata: {"text": "안녕"}\n\n')
    assert event == "delta"
    assert data == {"text": "안녕"}


def test_compare_transports_ignores_order_but_detects_candidate_difference():
    batch = {"turns": [{"index": 1, "product_ids": ["a", "b"]}]}
    stream_same = {"turns": [{"index": 1, "product_ids": ["b", "a"]}]}
    stream_diff = {"turns": [{"index": 1, "product_ids": ["a", "c"]}]}
    assert compare_transports(batch, stream_same) == []
    assert compare_transports(batch, stream_diff) == [{
        "turn": 1,
        "batch_product_ids": ["a", "b"],
        "stream_product_ids": ["a", "c"],
    }]


@pytest.mark.parametrize(
    ("passed", "no_mlflow"),
    [(True, False), (False, False), (False, True)],
)
def test_cli_logs_failures_unless_explicitly_disabled(
    tmp_path, monkeypatch, passed, no_mlflow,
):
    output = tmp_path / "multiturn.json"
    report = {
        "run": {"n_scenarios": 15, "transports": ["batch", "stream"],
                "code_sha": "abc123", "dataset_sha256": "dataset123"},
        "metrics": {"functional_failures": 0 if passed else 1,
                    "transport_differences": 0, "passed": passed},
        "scenarios": [],
    }
    monkeypatch.setattr(multiturn_eval, "run", AsyncMock(return_value=report))
    log = Mock(return_value=("logged", "run123"))
    monkeypatch.setattr(multiturn_eval, "log_report", log)
    args = ["run_multiturn_eval.py", "--out", str(output)]
    if no_mlflow:
        args.append("--no-mlflow")
    monkeypatch.setattr("sys.argv", args)

    assert multiturn_eval.main() == (0 if passed else 1)
    assert output.exists()
    if no_mlflow:
        log.assert_not_called()
    else:
        log.assert_called_once_with(output)
