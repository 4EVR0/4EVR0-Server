"""Multi-turn functional evaluation for batch and SSE recommendation paths.

The response wording may vary even at temperature 0, so this runner checks observable
service behavior instead of exact text: follow-up product reuse or category refinement,
contextual re-search, expired-history handling, Hanja leakage, errors, and batch/SSE parity.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.prompts import prompt_version  # noqa: E402
from app.repositories import conversation_store  # noqa: E402
from app.services.recommend_service import recommend, recommend_stream  # noqa: E402
from eval.hard_checks import check_response  # noqa: E402
from eval.mlflow_tracking import log_report  # noqa: E402

DEFAULT_DATASET = _REPO_ROOT / "eval" / "multiturn_dataset.jsonl"
DEFAULT_OUT = _REPO_ROOT / "eval" / "results" / "multiturn-latest.json"


def load_scenarios(path: Path) -> list[dict]:
    scenarios = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seen: set[str] = set()
    for row in scenarios:
        scenario_id = str(row.get("id") or "")
        if not scenario_id or scenario_id in seen:
            raise ValueError(f"missing or duplicate scenario id: {scenario_id!r}")
        seen.add(scenario_id)
        turns = row.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{scenario_id}: turns must be a non-empty list")
        for turn in turns:
            if turn.get("kind") not in {"new", "followup", "usage_order", "refine", "contextual_search", "missing_history"}:
                raise ValueError(f"{scenario_id}: invalid turn kind {turn.get('kind')!r}")
            if not str(turn.get("message") or "").strip():
                raise ValueError(f"{scenario_id}: empty message")
            if turn.get("kind") in {"refine", "contextual_search"} and not turn.get("expected_category"):
                raise ValueError(f"{scenario_id}: expected_category is required")
    return scenarios


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_sha() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def parse_sse_frame(frame: str) -> tuple[str, dict]:
    event = "message"
    data_lines: list[str] = []
    for line in frame.strip().splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        raise ValueError(f"SSE frame has no data: {frame!r}")
    return event, json.loads("".join(data_lines))


async def call_batch(session_id: str, message: str) -> dict:
    response = await recommend(session_id, message)
    return response.model_dump()


async def call_stream(session_id: str, message: str) -> dict:
    meta: dict | None = None
    chunks: list[str] = []
    finish_reason: str | None = None
    response_mode: str | None = None
    async for frame in recommend_stream(session_id, message):
        event, data = parse_sse_frame(frame)
        if event == "meta":
            meta = data
        elif event == "delta":
            chunks.append(str(data.get("text") or ""))
        elif event == "done":
            finish_reason = str(data.get("finish_reason") or "")
            response_mode = str(data.get("response_mode") or "")
        elif event == "error":
            raise RuntimeError(f"{data.get('error_code')}: {data.get('message')}")
    if meta is None:
        raise RuntimeError("stream ended without a meta event")
    return {
        "session_id": meta.get("session_id", session_id),
        "turn_id": meta.get("turn_id"),
        "ingredients": meta.get("ingredients") or [],
        "products": meta.get("products") or [],
        "response_text": "".join(chunks),
        "model_used": meta.get("model_used"),
        "finish_reason": finish_reason,
        "response_mode": response_mode,
    }


def product_ids(result: dict) -> list[str]:
    return [str(product.get("product_id")) for product in result.get("products", [])]


def evaluate_turn(turn: dict, result: dict, previous: dict | None) -> list[str]:
    failures = [failure.code for failure in check_response(turn, result)]
    response_text = str(result.get("response_text") or "")

    kind = turn["kind"]
    current_ids = set(product_ids(result))
    if kind == "followup":
        previous_ids = set(product_ids(previous or {}))
        if not previous_ids:
            if current_ids:
                failures.append("FOLLOWUP_WITHOUT_PREVIOUS_PRODUCTS")
            if "비교할 제품이 없습니다" not in response_text:
                failures.append("NO_PRODUCT_FOLLOWUP_MESSAGE_ABSENT")
        elif current_ids != previous_ids:
            failures.append("FOLLOWUP_PRODUCT_SET_CHANGED")
    elif kind == "usage_order":
        previous_products = (previous or {}).get("products", [])
        current_products = result.get("products", [])
        previous_ids = set(product_ids(previous or {}))
        if not previous_products:
            if current_products:
                failures.append("USAGE_ORDER_WITHOUT_PREVIOUS_PRODUCTS")
            if "비교할 제품이 없습니다" not in response_text:
                failures.append("NO_PRODUCT_FOLLOWUP_MESSAGE_ABSENT")
        else:
            stages = ({"스킨", "토너"}, {"앰플"}, {"세럼"}, {"크림"})

            def stage_index(product):
                return next((index for index, categories in enumerate(stages)
                             if product.get("category") in categories), None)

            expected = [index for index, categories in enumerate(stages)
                        if any(p.get("category") in categories and p.get("matched_ingredients")
                               for p in previous_products)]
            actual = [stage_index(product) for product in current_products]
            if not current_ids.issubset(previous_ids):
                failures.append("USAGE_ORDER_NEW_PRODUCT")
            if actual != expected or len(current_ids) != len(current_products):
                failures.append("USAGE_ORDER_STAGE_MISMATCH")
            if result.get("response_mode") != "followup_usage_order":
                failures.append("USAGE_ORDER_MODE_MISMATCH")
    elif kind == "refine":
        expected_category = turn.get("expected_category")
        previous_products = (previous or {}).get("products", [])
        if not previous_products:
            if current_ids:
                failures.append("REFINE_WITHOUT_PREVIOUS_PRODUCTS")
            if "비교할 제품이 없습니다" not in response_text:
                failures.append("NO_PRODUCT_REFINE_MESSAGE_ABSENT")
            return failures
        expected_ids = {str(p.get("product_id")) for p in previous_products
                        if p.get("category") == expected_category}
        if not expected_category:
            failures.append("REFINE_EXPECTED_CATEGORY_MISSING")
        elif current_ids != expected_ids:
            failures.append("REFINE_PRODUCT_SET_MISMATCH")
        if not expected_ids and "새로 찾아볼까요" not in response_text:
            failures.append("REFINE_NO_MATCH_MESSAGE_ABSENT")
    elif kind == "contextual_search":
        expected_category = turn.get("expected_category")
        if not expected_category:
            failures.append("CONTEXTUAL_SEARCH_EXPECTED_CATEGORY_MISSING")
        elif any(p.get("category") != expected_category for p in result.get("products", [])):
            failures.append("CONTEXTUAL_SEARCH_WRONG_CATEGORY")
    elif kind == "missing_history":
        if current_ids:
            failures.append("MISSING_HISTORY_RETURNED_PRODUCTS")
        if "이전 추천 내역을 찾지 못했어요" not in response_text:
            failures.append("MISSING_HISTORY_MESSAGE_ABSENT")
    return failures


async def run_scenario(scenario: dict, transport: str, run_id: str) -> dict:
    session_id = f"eval-mt-{run_id}-{scenario['id']}-{transport}"
    await conversation_store.clear(session_id)
    rows: list[dict] = []
    previous: dict | None = None
    try:
        for index, turn in enumerate(scenario["turns"], start=1):
            try:
                result = await (call_batch(session_id, turn["message"])
                                if transport == "batch"
                                else call_stream(session_id, turn["message"]))
                failures = evaluate_turn(turn, result, previous)
                row = {
                    "index": index,
                    "kind": turn["kind"],
                    "message": turn["message"],
                    "product_ids": product_ids(result),
                    "n_ingredients": len(result.get("ingredients", [])),
                    "response": result.get("response_text"),
                    "finish_reason": result.get("finish_reason"),
                    "response_mode": result.get("response_mode"),
                    "failures": failures,
                }
                previous = result
            except Exception as exc:  # keep the rest of the report inspectable
                row = {
                    "index": index,
                    "kind": turn["kind"],
                    "message": turn["message"],
                    "product_ids": [],
                    "n_ingredients": 0,
                    "response": "",
                    "finish_reason": None,
                    "response_mode": None,
                    "failures": ["REQUEST_ERROR"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
                previous = None
            rows.append(row)
    finally:
        await conversation_store.clear(session_id)
    return {
        "id": scenario["id"],
        "label": scenario.get("label"),
        "transport": transport,
        "session_id": session_id,
        "turns": rows,
    }


def compare_transports(batch: dict, stream: dict) -> list[dict]:
    differences: list[dict] = []
    for left, right in zip(batch["turns"], stream["turns"]):
        left_ids = set(left["product_ids"])
        right_ids = set(right["product_ids"])
        if left_ids != right_ids:
            differences.append({
                "turn": left["index"],
                "batch_product_ids": sorted(left_ids),
                "stream_product_ids": sorted(right_ids),
            })
    if len(batch["turns"]) != len(stream["turns"]):
        differences.append({"turn_count": [len(batch["turns"]), len(stream["turns"])]})
    return differences


async def run(args) -> dict:
    dataset_path = Path(args.dataset)
    scenarios = load_scenarios(dataset_path)
    if args.limit:
        scenarios = scenarios[:args.limit]
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    transports = [args.transport] if args.transport != "both" else ["batch", "stream"]

    results: list[dict] = []
    for scenario in scenarios:
        per_transport = [await run_scenario(scenario, transport, run_id) for transport in transports]
        row = {
            "id": scenario["id"],
            "label": scenario.get("label"),
            "results": {result["transport"]: result for result in per_transport},
            "transport_differences": [],
        }
        if len(per_transport) == 2:
            row["transport_differences"] = compare_transports(per_transport[0], per_transport[1])
        results.append(row)

    functional_failures = sum(
        len(turn["failures"])
        for scenario in results
        for result in scenario["results"].values()
        for turn in result["turns"]
    )
    transport_differences = sum(len(scenario["transport_differences"]) for scenario in results)
    hard_failure_count = functional_failures + transport_differences
    hard_failure_scenarios = sum(
        any(turn["failures"] for result in scenario["results"].values() for turn in result["turns"])
        or bool(scenario["transport_differences"])
        for scenario in results
    )
    return {
        "run": {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "code_sha": code_sha(),
            "dataset": str(dataset_path),
            "dataset_sha256": file_sha256(dataset_path),
            "model": settings.gpu_model,
            "gen_prompt": settings.gen_prompt_name,
            "gen_prompt_version": prompt_version(settings.gen_prompt_name),
            "cache_enabled": settings.recommend_cache_enabled,
            "transports": transports,
            "n_scenarios": len(scenarios),
        },
        "metrics": {
            "functional_failures": functional_failures,
            "transport_differences": transport_differences,
            "hard_failure_count": hard_failure_count,
            "hard_failure_scenarios": hard_failure_scenarios,
            "hard_failure_rate": round(hard_failure_scenarios / len(results), 4) if results else 0.0,
            "passed": functional_failures == 0 and transport_differences == 0,
        },
        "scenarios": results,
    }


def print_summary(report: dict) -> None:
    run_info = report["run"]
    metrics = report["metrics"]
    print("=" * 72)
    print("  MULTI-TURN BATCH/SSE EVALUATION")
    print("=" * 72)
    print(f"  scenarios={run_info['n_scenarios']}  transports={','.join(run_info['transports'])}")
    print(f"  code_sha={run_info['code_sha']}  dataset={run_info['dataset_sha256'][:8]}")
    print(f"  functional_failures={metrics['functional_failures']}")
    print(f"  transport_differences={metrics['transport_differences']}")
    print(f"  RESULT={'PASS' if metrics['passed'] else 'FAIL'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="multi-turn batch/SSE functional evaluation")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--transport", choices=["batch", "stream", "both"], default="both")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-mlflow", action="store_true", help="MLflow 기록 비활성화")
    args = parser.parse_args()

    report = asyncio.run(run(args))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(report)
    print(f"  report={out_path}")
    if not args.no_mlflow:
        status, run_id = log_report(out_path)
        print(f"  MLflow {status}: {run_id}")
    return 0 if report["metrics"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
