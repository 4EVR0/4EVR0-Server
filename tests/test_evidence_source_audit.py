import csv

import pytest

from eval.audit_evidence_sources import CLAIM_FIELDS, MAP_FIELDS, audit_sources


def write_batch(root, directory, claims, mappings):
    folder = root / directory
    folder.mkdir()
    for name, rows, required in (
        ("graph_claim.csv", claims, CLAIM_FIELDS),
        ("claim_effect_map.csv", mappings, MAP_FIELDS),
    ):
        fields = sorted(required | {key for row in rows for key in row})
        with (folder / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return folder


def claim(**changes):
    return {
        "batch_id": "batch-a", "claim_key": "shared-key", "ingredient_name": "Bakuchiol",
        "pmid": "123", "source_url": "https://pubmed.ncbi.nlm.nih.gov/123/",
        "source_sentence": "A combination formulation was tested in mice.",
        "claim_type": "efficacy", "evidence_direction": "supports",
        "eligibility_tier": "soft_graph", **changes,
    }


def mapping(**changes):
    return {"batch_id": "batch-a", "claim_key": "shared-key", "effect_code": "ANTI_AGING", **changes}


def test_source_and_context_are_preserved_without_automatic_approval(tmp_path):
    row = claim()
    write_batch(tmp_path, "a", [row], [mapping()])
    report = audit_sources(tmp_path, [" BAKUCHIOL "], "ANTI_AGING")
    result = report["claims"][0]
    assert result["source"]["source_sentence"] == row["source_sentence"]
    assert result["status"] == "needs_review"
    assert result["context_review_flags"] == [
        "NON_HUMAN_CONTEXT", "COMBINATION_CONTEXT", "FORMULATION_CONTEXT",
    ]
    assert not result["approved_for_serving"]
    assert not report["serving_graph_link_verified"]
    assert not report["publication_verified"]
    assert all(len(source["sha256"]) == 64 for source in report["inputs"])


def test_mappings_do_not_cross_batch_or_directory_boundaries(tmp_path):
    write_batch(tmp_path, "a", [claim()], [mapping()])
    write_batch(tmp_path, "b", [claim(batch_id="batch-b")], [mapping()])
    report = audit_sources(tmp_path, ["BAKUCHIOL"], "ANTI_AGING")
    assert report["summary"][0]["review_candidates"] == 1
    assert report["claims"][1]["blocking_issues"] == ["REQUESTED_EFFECT_NOT_MAPPED"]


def test_family_name_is_not_promoted_to_specific_inci(tmp_path):
    write_batch(tmp_path, "a", [claim(ingredient_name="Ceramide")], [mapping()])
    report = audit_sources(tmp_path, ["CERAMIDE NP"], "ANTI_AGING")
    assert report["claims"] == []
    assert report["summary"][0]["exact_match_rows"] == 0


@pytest.mark.parametrize("changes, issue", [
    ({"source_url": "https://pubmed.ncbi.nlm.nih.gov/456/"}, "PMID_URL_MISMATCH"),
    ({"source_sentence": "", "claim_text": "A generated summary."}, "MISSING_SOURCE_SENTENCE"),
    ({"claim_type": "safety"}, "NOT_EFFICACY_CLAIM"),
    ({"evidence_direction": "contradicts"}, "NOT_SUPPORTING_CLAIM"),
    ({"eligibility_tier": "recommendation_only"}, "NOT_GRAPH_ELIGIBLE"),
    ({"ingredient_detection_suspect": "True"}, "SUSPECT_INGREDIENT_DETECTION"),
])
def test_incomplete_or_inapplicable_evidence_is_blocked(tmp_path, changes, issue):
    write_batch(tmp_path, "a", [claim(**changes)], [mapping()])
    report = audit_sources(tmp_path, ["BAKUCHIOL"], "ANTI_AGING")
    assert issue in report["claims"][0]["blocking_issues"]
    assert report["summary"][0]["review_candidates"] == 0


def test_no_context_flag_still_requires_review_and_pmids_are_deduplicated(tmp_path):
    write_batch(tmp_path, "a", [claim(source_sentence="An outcome was reported.")], [mapping()])
    write_batch(tmp_path, "b", [claim(source_sentence="An outcome was reported.")], [mapping()])
    report = audit_sources(tmp_path, ["BAKUCHIOL"], "ANTI_AGING")
    assert report["summary"][0]["review_candidates"] == 2
    assert report["summary"][0]["candidate_pmids"] == ["123"]
    assert all(c["status"] == "needs_review" and not c["approved_for_serving"] for c in report["claims"])


def test_missing_mapping_file_does_not_silently_hide_a_batch(tmp_path):
    folder = write_batch(tmp_path, "a", [claim()], [mapping()])
    (folder / "claim_effect_map.csv").unlink()
    with pytest.raises(FileNotFoundError):
        audit_sources(tmp_path, ["BAKUCHIOL"], "ANTI_AGING")


def test_duplicate_claim_identity_is_rejected(tmp_path):
    write_batch(tmp_path, "a", [claim(), claim(source_sentence="Different sentence.")], [mapping()])
    with pytest.raises(ValueError, match="duplicate selected claim identity"):
        audit_sources(tmp_path, ["BAKUCHIOL"], "ANTI_AGING")
