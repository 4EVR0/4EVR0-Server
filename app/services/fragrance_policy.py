"""Product-label evidence and ingredient rationale have different meanings."""

from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlparse

from app.core.config import settings
from app.domain.enums import Constraint
from app.services.taxonomy_normalization_service import normalize_constraints

# These ingredients are not positive recommendation rationales in this service.
# Their presence in a product's inventory remains factual and can still be shown.
FRAGRANCE_RATIONALE_EXCLUSIONS = frozenset({"LINALOOL", "FARNESOL", "LIMONENE"})


def eligible_rationale(name: str | None) -> bool:
    """Positive recommendation evidence only; never filters product inventory."""
    return str(name or "").strip().upper() not in FRAGRANCE_RATIONALE_EXCLUSIONS

_NAMES = re.compile(r"리날룰|파네솔|리모넨|\b(?:linalool|farnesol|limonene)\b", re.I)
_FREE = re.compile(r"무향(?:료)?|향료(?:가|를|는)?\s*(?:없|없는|미포함|무첨가|제외|빼)|향(?:이)?\s*없는|fragrance[-\s]*free|without\s+(?:fragrance|parfum)", re.I)
_WITHDRAW = re.compile(r"무향(?:료)?(?:이|가)?\s*(?:아니어도|필요\s*없|상관\s*없)|향료(?:가|는)?\s*(?:있어도|상관\s*없)", re.I)


def fragrance_preference(message: str) -> bool | None:
    remaining = _WITHDRAW.sub("", message)
    if _FREE.search(remaining):
        return True
    return False if _WITHDRAW.search(message) else None


def merge_fragrance_constraint(message: str, constraints: list[Constraint]) -> list[Constraint]:
    preference = fragrance_preference(message)
    result = list(constraints)
    # A combined request must not silently satisfy only its fragrance condition.
    for item in normalize_constraints(re.sub(r"\s+", "", message).lower()):
        if item != Constraint.FRAGRANCE_FREE and item not in result:
            result.append(item)
    if preference is True and Constraint.FRAGRANCE_FREE not in result:
        result.append(Constraint.FRAGRANCE_FREE)
    elif preference is False:
        result = [item for item in result if item != Constraint.FRAGRANCE_FREE]
    return result


def mentions_excluded_rationale(text: str) -> bool:
    """Conservative output guard for generated recommendations, not inventory tables."""
    return bool(_NAMES.search(text))


def parse_evidence(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _url(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _recent(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - timedelta(days=settings.fragrance_evidence_max_age_days) <= date <= now
    except ValueError:
        return False


def fragrance_decision(product_id: str, value) -> str:
    """Return present / verified_claim / unknown. Absence of a label never suffices."""
    evidence = parse_evidence(value)
    if evidence.get("product_id") != product_id:
        return "unknown"
    if evidence.get("status") == "present" or evidence.get("present_terms"):
        return "present"
    if (evidence.get("schema_version") != 1 or evidence.get("status") != "not_listed"
            or evidence.get("related_terms") != []
            or evidence.get("present_terms") != []
            or not _url(evidence.get("source_url")) or not _recent(evidence.get("observed_at"))):
        return "unknown"
    digest = evidence.get("label_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return "unknown"
    claim = evidence.get("manufacturer_claim")
    if not isinstance(claim, dict):
        return "unknown"
    if (claim.get("claim") != "no_added_fragrance" or claim.get("product_id") != product_id
            or claim.get("label_sha256") != digest
            or claim.get("label_reviewed_complete") is not True
            or not _url(claim.get("source_url")) or not _recent(claim.get("reviewed_at"))
            or not isinstance(claim.get("quote"), str) or not claim["quote"].strip()
            or not isinstance(claim.get("reviewed_by"), str) or not claim["reviewed_by"].strip()):
        return "unknown"
    return "verified_claim"
