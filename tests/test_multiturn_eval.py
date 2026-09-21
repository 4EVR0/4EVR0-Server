from pathlib import Path

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
    assert "followup_product_set_changed" in failures


def test_missing_history_contract():
    ok = _result([], "이전 추천 내역을 찾지 못했어요. 다시 알려주세요.")
    assert evaluate_turn({"kind": "missing_history"}, ok, None) == []


def test_hanja_leak_is_deterministic_failure():
    failures = evaluate_turn({"kind": "new"}, _result([], "피肤 응답"), None)
    assert "hanja_leak" in failures


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
