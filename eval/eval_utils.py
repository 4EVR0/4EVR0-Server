"""Shared validation and statistics helpers for offline evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from pathlib import Path

from app.domain.enums import Concern, Constraint, SkinType

PROFILE_FIELDS = ("skin_types", "concerns", "constraints")
VALID_PROFILE_VALUES = {
    "skin_types": {item.value for item in SkinType},
    "concerns": {item.value for item in Concern},
    "constraints": {item.value for item in Constraint},
}


def load_dataset(path: Path) -> list[dict]:
    """Load and strictly validate the shared JSONL evaluation dataset."""
    cases: list[dict] = []
    seen_ids: set[int | str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc

        missing = {"id", "message", *PROFILE_FIELDS} - case.keys()
        if missing:
            raise ValueError(f"{path}:{line_number}: missing fields: {sorted(missing)}")
        if case["id"] in seen_ids:
            raise ValueError(f"{path}:{line_number}: duplicate id: {case['id']}")
        if not isinstance(case["message"], str) or not case["message"].strip():
            raise ValueError(f"{path}:{line_number}: message must be a non-empty string")

        for field in PROFILE_FIELDS:
            values = case[field]
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"{path}:{line_number}: {field} must be a list of strings")
            invalid = set(values) - VALID_PROFILE_VALUES[field]
            if invalid:
                raise ValueError(f"{path}:{line_number}: invalid {field}: {sorted(invalid)}")
            if len(values) != len(set(values)):
                raise ValueError(f"{path}:{line_number}: duplicate values in {field}")

        seen_ids.add(case["id"])
        cases.append(case)

    if not cases:
        raise ValueError(f"{path}: dataset is empty")
    return cases


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bootstrap_mean_ci(
    values: list[float],
    *,
    confidence: float = 0.95,
    samples: int = 2_000,
    seed: int = 23,
) -> tuple[float, float] | None:
    """Return a deterministic percentile bootstrap CI for a sample mean."""
    if not values:
        return None
    if len(values) == 1:
        value = round(float(values[0]), 3)
        return value, value

    rng = random.Random(seed)
    means = sorted(
        statistics.mean(rng.choices(values, k=len(values)))
        for _ in range(samples)
    )
    tail = (1.0 - confidence) / 2.0
    low_index = max(0, math.floor(tail * samples))
    high_index = min(samples - 1, math.ceil((1.0 - tail) * samples) - 1)
    return round(means[low_index], 3), round(means[high_index], 3)


def pearson_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = statistics.mean(left), statistics.mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    return round(numerator / denominator, 4) if denominator else None


def _ranks(values: list[float]) -> list[float]:
    """Return average ranks for ties (1-based)."""
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position + 1
        while end < len(indexed) and indexed[end][1] == indexed[position][1]:
            end += 1
        average_rank = ((position + 1) + end) / 2.0
        for index, _ in indexed[position:end]:
            ranks[index] = average_rank
        position = end
    return ranks


def spearman_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    return pearson_correlation(_ranks(left), _ranks(right))
