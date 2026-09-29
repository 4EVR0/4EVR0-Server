import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from eval import run_dictionary_pilot as pilot
from app.services import recommend_service as service


def test_ranking_comparison_keeps_matches_and_product_order():
    base = [{"product_id": "p1", "matched_count": 1, "image_url": "expired"}]
    on = [{**base[0], "image_url": "fresh", "ingredient_explanations": [{"name": "GLYCERIN"}]}]
    assert pilot.ranking_evidence(base) == pilot.ranking_evidence(on)
    on[0]["matched_count"] = 2
    assert pilot.ranking_evidence(base) != pilot.ranking_evidence(on)


def test_paired_runner_saves_exposure_raw_outputs_and_shared_evidence(tmp_path):
    source = tmp_path / "source.json"
    output = tmp_path / "paired.json"
    product = {"product_id": "p1", "product_name": "테스트 크림", "brand": "테스트",
               "category": "크림", "matched_count": 1, "matched_ingredients": ["CAFFEINE"]}
    ingredients = [{"name": "CAFFEINE", "kor_name": "카페인", "claim": "Hydrating"}]
    snapshots = {
        "extract_with_fallback": [{"concerns": ["DRY_SKIN"]}, "llm"],
        "query_ingredients_by_effects": ingredients, "apply_caution_filter": ingredients, "select_products": [product],
    }
    source.write_text(json.dumps({"run": {"code_sha": "source-sha"}, "pairs": [
        {"id": "case1", "message": "건조해요", "kind": "target", "snapshot": [
            {"function": name, "result": result} for name, result in snapshots.items()]}]}))

    async def generate(message, ingredients, products, system_prompt):
        service._compose_user_content(message, ingredients, products)
        return service._build_grounded_product_response(message, ingredients, products)

    log = Mock(return_value=("logged", "test-run"))
    with (
        patch.object(pilot.subprocess, "check_output", side_effect=["", "current-sha"]),
        patch.object(pilot, "_check_llm", AsyncMock(return_value="ok")),
        patch.object(pilot, "close_driver", AsyncMock()),
        patch.object(pilot, "log_report", log),
        patch.object(pilot, "default_tracking_uri", return_value="sqlite:///test.db"),
        patch.object(service, "query_product_ingredient_inventory", AsyncMock(return_value={
            "p1": [{"name": "GLYCERIN"}]})),
        patch.object(service, "_build_llm_response", generate),
    ):
        asyncio.run(pilot.run(source, output, 123))
    result = json.loads(output.read_text())
    pair = result["pairs"][0]
    assert result["run"]["status"] == "completed"
    assert result["run"]["code_sha"] == "current-sha"
    assert result["metrics"]["treatment_exposed_pairs"] == 1
    assert result["metrics"]["identical_final_pairs"] == 0
    assert set(pair["display_key"].values()) == {"off", "on"}
    assert "글리세린" in pair["responses"]["on"]["response_text"]
    assert "글리세린" in pair["evidence"]["on"]["products"]
    assert "GLYCERIN" in pair["generation_context"]["on"]
    assert pair["hard_failures"] == {"off": [], "on": []}
    log.assert_called_once()


def test_unavailable_gpu_never_creates_or_logs_a_report(tmp_path):
    output = tmp_path / "report.json"
    log = Mock()
    with patch.object(pilot.subprocess, "check_output", return_value=""), \
         patch.object(pilot, "_check_llm", AsyncMock(return_value="error")), \
         patch.object(pilot, "log_report", log):
        with pytest.raises(RuntimeError, match="준비되지"):
            asyncio.run(pilot.run(tmp_path / "source.json", output, 123))
    assert not output.exists()
    log.assert_not_called()
