"""Trace local Gold claims for review; never promote them to serving evidence.

Uses only local CSVs. Exact ingredient matching deliberately leaves aliases and
ingredient families unresolved. A claim's presence in Gold does not prove that
it contributed to the currently served Neo4j edge or describes a retail product.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


CLAIM_FIELDS = {
    "batch_id", "claim_key", "ingredient_name", "pmid", "source_url",
    "source_sentence", "claim_type", "evidence_direction", "eligibility_tier",
}
MAP_FIELDS = {"batch_id", "claim_key", "effect_code"}
CONTEXT_PATTERNS = {
    "NON_HUMAN_CONTEXT": r"\b(mice|mouse|murine|rats?|in vitro|HaCaT|cell cultures?)\b",
    "COMBINATION_CONTEXT": r"\b(combination|combined|regimen|synerg\w*)\b",
    "FORMULATION_CONTEXT": r"\b(formulation|liposom\w*|dendrimers?|encapsulat\w*)\b",
    "TOLERABILITY_CONTEXT": r"\b(irritation|intolerance|tolerability|tolerated|sensitive)\b",
}


def _name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def _source_issues(row: dict[str, str], effects: set[str], effect: str) -> list[str]:
    issues = []
    if not row["batch_id"] or not row["claim_key"]:
        issues.append("MISSING_CLAIM_IDENTITY")
    if effect not in effects:
        issues.append("REQUESTED_EFFECT_NOT_MAPPED")
    if not row["source_sentence"]:
        issues.append("MISSING_SOURCE_SENTENCE")
    pmid = row["pmid"]
    if not re.fullmatch(r"[1-9][0-9]*", pmid):
        issues.append("INVALID_PMID")
    try:
        url = urlsplit(row["source_url"])
        matches = (url.scheme == "https" and url.netloc == "pubmed.ncbi.nlm.nih.gov"
                   and url.path.rstrip("/") == f"/{pmid}" and not url.query and not url.fragment)
    except ValueError:
        matches = False
    if not matches:
        issues.append("PMID_URL_MISMATCH")
    if row["claim_type"].casefold() != "efficacy":
        issues.append("NOT_EFFICACY_CLAIM")
    if row["evidence_direction"].casefold() != "supports":
        issues.append("NOT_SUPPORTING_CLAIM")
    if row["eligibility_tier"] not in {"strict_graph", "soft_graph"}:
        issues.append("NOT_GRAPH_ELIGIBLE")
    if row.get("ingredient_detection_suspect", "").casefold() == "true":
        issues.append("SUSPECT_INGREDIENT_DETECTION")
    if row.get("exclusion_reason", "").casefold() not in {"", "n/a", "none", "nan"}:
        issues.append("UPSTREAM_EXCLUSION")
    return issues


def audit_sources(claims_root: Path, ingredients: list[str], effect: str) -> dict:
    requested = {_name(name): name.strip() for name in ingredients if name.strip()}
    if not requested or not effect.strip():
        raise ValueError("At least one ingredient and a non-empty effect are required")
    effect = effect.strip().upper()
    paths = sorted(claims_root.glob("*/graph_claim.csv"))
    if not paths:
        raise ValueError(f"{claims_root}: no batch graph_claim.csv files")
    sources, claims = [], []
    for path in paths:
        map_path = path.with_name("claim_effect_map.csv")
        # Join within each batch directory AND on the batch ID. Repeated claim
        # keys in different exports must never borrow one another's mappings.
        effect_map: dict[tuple[str, str], set[str]] = {}
        for row in _rows(map_path, MAP_FIELDS):
            if not row["batch_id"] or not row["claim_key"] or not row["effect_code"]:
                raise ValueError(f"{map_path}: incomplete effect mapping identity")
            effect_map.setdefault((row["batch_id"], row["claim_key"]), set()).add(row["effect_code"])
        for source in (path, map_path):
            sources.append({"path": str(source.resolve()),
                            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        seen: set[tuple[str, str]] = set()
        for row in _rows(path, CLAIM_FIELDS):
            ingredient = requested.get(_name(row["ingredient_name"]))
            if ingredient is None:
                continue
            identity = (row["batch_id"], row["claim_key"])
            if identity in seen:
                raise ValueError(f"{path}: duplicate selected claim identity {identity}")
            seen.add(identity)
            effects = effect_map.get(identity, set())
            issues = _source_issues(row, effects, effect)
            context = " ".join((row["source_sentence"], row.get("title", "")))
            flags = [name for name, pattern in CONTEXT_PATTERNS.items()
                     if re.search(pattern, context, re.IGNORECASE)]
            claims.append({
                "requested_ingredient": ingredient,
                "source_file": str(path.resolve()),
                "source": row,
                "mapped_effects": sorted(effects),
                "status": "blocked" if issues else "needs_review",
                "blocking_issues": issues,
                "context_review_flags": flags,
                "approved_for_serving": False,
            })
    summary = []
    for ingredient in requested.values():
        matched = [c for c in claims if c["requested_ingredient"] == ingredient]
        candidates = [c for c in matched if c["status"] == "needs_review"]
        summary.append({
            "ingredient": ingredient, "exact_match_rows": len(matched),
            "review_candidates": len(candidates),
            "candidate_pmids": sorted({c["source"]["pmid"] for c in candidates}),
            "blocking_issue_counts": dict(Counter(i for c in matched for i in c["blocking_issues"])),
        })
    return {
        "schema_version": 1, "audit_kind": "local_gold_source_review",
        "audit_tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "effect": effect, "ingredient_matching": "casefold_whitespace_exact",
        "serving_graph_link_verified": False, "publication_verified": False,
        "limitations": [
            "Local exports may not be the batches loaded into the serving graph.",
            "A matching PMID URL is syntactic validation, not publication verification.",
            "Context flags are heuristic review cues; absence of flags is not approval.",
            "Candidate PMIDs across batches are not the serving graph paper count.",
            "Review population, formulation, outcome and ingredient identity before using any sentence.",
        ],
        "inputs": sources, "summary": summary, "claims": claims,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims-root", type=Path, required=True)
    parser.add_argument("--ingredient", action="append", required=True)
    parser.add_argument("--effect", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit_sources(args.claims_root, args.ingredient, args.effect)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Audits are local diagnostics, not live model runs or judge scores.
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Saved local source audit: {args.out}")


if __name__ == "__main__":
    main()
