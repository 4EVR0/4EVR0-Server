"""Frozen retrieval + live GPU paired pilot; no external Judge or graph writes.

Input is a local report containing cases and per-function retrieval snapshots.
Output includes raw drafts, guarded answers, displayed evidence and blind ordering.
"""

import argparse
import asyncio
import copy
import hashlib
import json
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
import random
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.health import _check_llm
from app.clients.neo4j_client import close_driver
from app.core.config import settings
from app.domain.user import UserProfile
from app.services import recommend_service as service
from app.services.ingredient_explanations import CARD_SHA256, POLICY_SHA256, generation_system_prompt, product_explanations
from eval.hard_checks import check_response
from eval.mlflow_tracking import default_tracking_uri, log_report
from eval.run_response_eval import render_evidence_context


def serial(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value"):
        return value.value
    raise TypeError(type(value).__name__)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=serial).encode()).hexdigest()


def ranking_evidence(rows):
    """Ignore only expiring presentation URLs and the intentional treatment field."""
    return [{k: v for k, v in row.items() if k not in {"image_url", "ingredient_explanations"}} for row in rows]


async def run(snapshot_path: Path, output: Path, seed: int):
    if output.exists():
        raise ValueError("기존 실행 결과는 덮어쓰지 않습니다. 새 출력 경로를 지정하세요.")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True).strip():
        raise ValueError("평가 코드를 먼저 커밋해 실행 SHA를 확정하세요.")
    if await _check_llm() != "ok":
        raise RuntimeError("설정된 GPU 모델이 준비되지 않아 평가를 시작하지 않았습니다.")
    source_bytes = snapshot_path.read_bytes()
    source = json.loads(source_bytes)
    report = {"run": {
        "timestamp": datetime.now(timezone.utc).isoformat(), "generator_model": settings.gpu_model,
        "code_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "retrieval_source_sha": source.get("run", {}).get("code_sha"),
        "snapshot_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "pilot_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dictionary_sha256": CARD_SHA256, "system_prompt_sha256": digest(service._SYSTEM_PROMPT),
        "dictionary_policy_sha256": POLICY_SHA256,
        "model_revision": "unverified; same configured live endpoint for all pairs",
        "gen_temperature": 0, "gen_max_tokens": settings.gen_max_tokens, "seed": seed,
        "scope": "frozen retrieval, live generation; confirmed constituents + dictionary explanations",
        "external_judge": False, "production_graph_write": False, "human_review": "pending",
        "status": "running",
    }, "metrics": {}, "pairs": []}
    rng = random.Random(seed)
    try:
        with patch.object(settings, "conversation_enabled", False), \
             patch.object(settings, "recommend_cache_enabled", False), \
             patch.object(settings, "gen_temperature", 0):
            for case in source["pairs"]:
                snapshots = {row["function"]: row for row in case["snapshot"]}
                profile_row, extraction_method = snapshots["extract_with_fallback"]["result"]
                arm = {"value": "off"}
                drafts, contexts, system_prompts, inventory_snapshot = {}, {}, {}, {}
                original_compose, original_build = service._compose_user_content, service._build_llm_response
                original_inventory = service.query_product_ingredient_inventory

                def replay(name):
                    async def call(*args, **kwargs):
                        value = copy.deepcopy(snapshots[name]["result"])
                        if name == "extract_with_fallback":
                            return UserProfile.model_validate(value[0]), value[1]
                        return value
                    return call

                async def inventory(product_ids):
                    key = tuple(product_ids)
                    if key not in inventory_snapshot:
                        inventory_snapshot[key] = copy.deepcopy(await original_inventory(product_ids))
                    return copy.deepcopy(inventory_snapshot[key])

                def compose(message, ingredients, products):
                    content = original_compose(message, ingredients, products)
                    contexts[arm["value"]] = content
                    system_prompts[arm["value"]] = generation_system_prompt(service._SYSTEM_PROMPT, products)
                    return content

                async def build(*args, **kwargs):
                    text = await original_build(*args, **kwargs)
                    drafts[arm["value"]] = text
                    return text

                with ExitStack() as stack:
                    for name in ("extract_with_fallback", "query_ingredients_by_effects", "apply_caution_filter", "select_products"):
                        stack.enter_context(patch.object(service, name, replay(name)))
                    stack.enter_context(patch.object(service, "query_product_ingredient_inventory", inventory))
                    stack.enter_context(patch.object(service, "_compose_user_content", compose))
                    stack.enter_context(patch.object(service, "_build_llm_response", build))
                    order = ["off", "on"]
                    rng.shuffle(order)
                    responses, evidence, failures = {}, {}, {}
                    for variant in order:
                        arm["value"] = variant
                        with patch.object(settings, "dictionary_explanations_enabled", variant == "on"):
                            result = await service.recommend(f"dictionary-{case['id']}-{variant}", case["message"])
                        responses[variant] = result.model_dump(mode="json")
                        evidence[variant] = render_evidence_context(result.ingredients, result.products,
                            include_verified_studies=True, response_text=result.response_text)
                        failures[variant] = [failure.as_dict() for failure in check_response(
                            {"message": case["message"], **profile_row}, result)]
                        print(case["id"], variant, result.response_mode, flush=True)
                    assert responses["off"]["ingredients"] == responses["on"]["ingredients"]
                    assert ranking_evidence(responses["off"]["products"]) == ranking_evidence(responses["on"]["products"])
                exposure = sorted({card.name for product in responses["on"]["products"]
                                   for card in product_explanations(product)})
                input_cards = [name for name in exposure if name in contexts.get("on", "")]
                final_card_mentions = sorted({card.name for product in responses["on"]["products"]
                    for card in product_explanations(product)
                    if card.kor_name in responses["on"]["response_text"] or card.name in responses["on"]["response_text"]})
                display = ["off", "on"]
                rng.shuffle(display)
                report["pairs"].append({
                    "id": case["id"], "message": case["message"], "kind": case["kind"],
                    "profile": profile_row, "extraction_method": extraction_method,
                    "generation_order": order, "display_key": dict(zip(("A", "B"), display)),
                    "retrieval_snapshot": case["snapshot"], "inventory_snapshot": [
                        {"product_ids": list(key), "result": value} for key, value in inventory_snapshot.items()],
                    "matching_cards": exposure, "input_cards": input_cards,
                    "final_card_mentions": final_card_mentions, "generation_context": contexts, "drafts": drafts,
                    "generation_system_prompts": system_prompts,
                    "responses": responses, "evidence": evidence, "hard_failures": failures,
                    "identical_final": responses["off"]["response_text"] == responses["on"]["response_text"],
                })
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=serial), encoding="utf-8")
        pairs = report["pairs"]
        report["metrics"] = {
            "n_pairs": len(pairs), "identical_final_pairs": sum(pair["identical_final"] for pair in pairs),
            "treatment_exposed_pairs": sum(bool(pair["input_cards"]) for pair in pairs),
            "confirmed_card_pairs": sum(bool(pair["matching_cards"]) for pair in pairs),
            "final_card_mention_pairs": sum(bool(pair["final_card_mentions"]) for pair in pairs),
            "off_hard_failure_cases": sum(bool(pair["hard_failures"]["off"]) for pair in pairs),
            "on_hard_failure_cases": sum(bool(pair["hard_failures"]["on"]) for pair in pairs),
        }
        report["run"]["status"] = "completed"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=serial), encoding="utf-8")
        print("MLflow", log_report(output, tracking_uri=default_tracking_uri()), flush=True)
    finally:
        await close_driver()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260929)
    args = parser.parse_args()
    asyncio.run(run(args.snapshot_report, args.output, args.seed))


if __name__ == "__main__":
    main()
