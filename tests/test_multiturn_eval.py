import asyncio
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
    assert len(scenarios) == 18
    kinds = {turn["kind"] for scenario in scenarios for turn in scenario["turns"]}
    assert kinds == {"new", "followup", "usage_order", "refine", "contextual_search", "missing_history"}


def test_usage_order_requires_one_previous_product_per_present_stage_in_order():
    previous = {"products": [
        {"product_id": "s", "category": "세럼", "matched_ingredients": ["X"]},
        {"product_id": "a", "category": "앰플", "matched_ingredients": ["X"]},
        {"product_id": "a2", "category": "앰플", "matched_ingredients": ["Y"]},
        {"product_id": "c", "category": "크림", "matched_ingredients": ["X"]},
    ]}
    turn = {"kind": "usage_order"}
    correct = {"products": [previous["products"][1], previous["products"][0], previous["products"][3]],
               "ingredients": [], "response_text": "추천 제품", "response_mode": "followup_usage_order"}
    assert evaluate_turn(turn, correct, previous) == []
    wrong_order = {**correct, "products": list(reversed(correct["products"]))}
    assert "USAGE_ORDER_STAGE_MISMATCH" in evaluate_turn(turn, wrong_order, previous)
    invented = {**correct, "products": [{"product_id": "new", "category": "토너"}]}
    assert "USAGE_ORDER_NEW_PRODUCT" in evaluate_turn(turn, invented, previous)
    unmatched_previous = {"products": [*previous["products"],
                                       {"product_id": "t", "category": "토너", "matched_ingredients": []}]}
    assert evaluate_turn(turn, correct, unmatched_previous) == []


def test_refine_requires_exact_previous_category_subset():
    previous = {"products": [{"product_id": "t", "category": "토너"},
                             {"product_id": "c", "category": "크림"}]}
    turn = {"kind": "refine", "expected_category": "토너"}
    assert evaluate_turn(turn, _result(["t"]), previous) == []
    assert "REFINE_PRODUCT_SET_MISMATCH" in evaluate_turn(turn, _result(["c"]), previous)


def test_refine_no_match_requires_confirmation_and_no_cards():
    previous = {"products": [{"product_id": "c", "category": "크림"}]}
    turn = {"kind": "refine", "expected_category": "토너"}
    assert evaluate_turn(turn, _result([], "앞서 보여드린 제품 중 토너 제품은 없어요. 새로 찾아볼까요?"), previous) == []
    assert "REFINE_NO_MATCH_MESSAGE_ABSENT" in evaluate_turn(turn, _result([]), previous)


def test_contextual_search_uses_only_requested_category():
    turn = {"kind": "contextual_search", "expected_category": "토너"}
    assert evaluate_turn(turn, {"products": [{"product_id": "t", "category": "토너"}],
                                "ingredients": [], "response_text": "추천"}, None) == []
    assert "CONTEXTUAL_SEARCH_WRONG_CATEGORY" in evaluate_turn(
        turn, {"products": [{"product_id": "c", "category": "크림"}],
               "ingredients": [], "response_text": "추천"}, None)


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


def test_stream_report_preserves_response_mode(monkeypatch):
    async def fake_stream(_session_id, _message):
        yield 'event: meta\ndata: {"products": [], "ingredients": []}\n\n'
        yield 'event: delta\ndata: {"text": "안내"}\n\n'
        yield 'event: done\ndata: {"finish_reason": "conversation", "response_mode": "followup_quality_fallback"}\n\n'

    monkeypatch.setattr(multiturn_eval, "recommend_stream", fake_stream)
    result = asyncio.run(multiturn_eval.call_stream("session-1", "사용 순서 알려줘"))
    assert result["response_mode"] == "followup_quality_fallback"


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
