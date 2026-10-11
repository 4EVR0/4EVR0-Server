"""이 프로세스가 답변을 만들 때 쓰는 버전 묶음(릴리스)과 그 식별자.

답변마다 어떤 모델·앱·프롬프트·그래프·캐시 설정에서 나왔는지 추적하려고 쓴다.
비밀값(키·비밀번호·접속 주소)은 담지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from app.core.config import settings
from app.prompts import load_prompt

logger = logging.getLogger(__name__)

_current: dict[str, Any] | None = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def static_components() -> dict[str, Any]:
    """그래프 조회 없이 정해지는 구성 요소."""
    from app.repositories.recommend_cache import _KEY_PREFIX
    from app.services.ingredient_claim_guard import POLICY_VERSION
    from app.services.ingredient_explanations import CARD_SHA256, POLICY_SHA256
    from app.services.recommend_service import _FOLLOWUP_SYSTEM

    return {
        "app_version": settings.app_version,
        "vcs_ref": os.environ.get("APP_VCS_REF", "unknown"),
        "gpu_model": settings.gpu_model,
        "gen_prompt": settings.gen_prompt_name,
        "gen_prompt_sha256": _sha256(load_prompt(settings.gen_prompt_name)),
        "followup_prompt_sha256": _sha256(_FOLLOWUP_SYSTEM),
        "gen_temperature": settings.gen_temperature,
        "gen_max_tokens": settings.gen_max_tokens,
        "cache_namespace": _KEY_PREFIX,
        "claim_guard_policy": POLICY_VERSION,
        "dictionary_explanations": settings.dictionary_explanations_enabled,
        "dictionary_card_sha256": CARD_SHA256 if settings.dictionary_explanations_enabled else None,
        "dictionary_policy_sha256": POLICY_SHA256 if settings.dictionary_explanations_enabled else None,
    }


def release_id(components: dict[str, Any]) -> str:
    return _sha256(json.dumps(components, sort_keys=True, ensure_ascii=False, default=str))


def build(graph: dict[str, Any] | None) -> dict[str, Any]:
    components = {**static_components(), "graph": graph or {"status": "unknown"}}
    return {"release_id": release_id(components), "components": components}


def set_current(info: dict[str, Any]) -> None:
    global _current
    _current = info
    logger.info("release %s %s", info["release_id"], json.dumps(info["components"], ensure_ascii=False, default=str))


def current() -> dict[str, Any] | None:
    return _current


def current_id() -> str | None:
    return _current["release_id"] if _current else None


async def register() -> None:
    """기동 시 릴리스를 계산해 저장한다. 실패해도 서비스는 계속한다(턴 기록만 빠진다)."""
    from app.clients.neo4j_client import query_graph_version
    from app.repositories import turn_event_repository

    info = build(await query_graph_version())
    try:
        await turn_event_repository.ensure_tables()
        await turn_event_repository.upsert_release(info["release_id"], info["components"])
    except Exception as exc:
        logger.warning("release registration failed: %s", exc)
        return
    set_current(info)
