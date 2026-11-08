"""개발자용 내부 조회 API(읽기 전용). 수집된 질문·답변을 응답 방식으로 걸러 본다.

- 기본 꺼짐(INTERNAL_VIEWER_ENABLED). 켜도 INTERNAL_ALLOWED_NETWORKS(기본 Tailscale 100.64.0.0/10)에서
  직접 온 요청만 받는다. 공개 도메인(Caddy)을 거친 요청은 실제 접속 IP가 공인 IP라 404다.
- 운영에서는 베타 EC2의 내부 포트를 Tailscale로만 접근한다(보안 그룹 미개방).
- 응답에 가명 사용자 ID(subject_id)는 넣지 않는다. 원문을 로그에 남기지 않는다.
"""

import ipaddress
import json
import logging
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.core.config import settings
from app.repositories import data_collection_repository as repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)
MAX_LIMIT = 200


@lru_cache(maxsize=8)
def _networks(raw: str) -> tuple:
    return tuple(ipaddress.ip_network(part.strip()) for part in raw.split(",") if part.strip())


def _allow(request: Request) -> None:
    if not settings.internal_viewer_enabled:
        raise HTTPException(status_code=404)
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise HTTPException(status_code=404)
    if not any(address in network for network in _networks(settings.internal_allowed_networks)):
        raise HTTPException(status_code=404)


@router.get("/turn-records/modes")
async def turn_record_modes(request: Request, response: Response, days: int = Query(7, ge=1, le=30)) -> dict:
    _allow(request)
    response.headers["Cache-Control"] = "no-store"
    return {"days": days, "modes": await repo.count_turn_records_by_mode(days)}


@router.get("/turn-records")
async def turn_records(request: Request, response: Response,
                       mode: list[str] | None = Query(None, description="응답 방식(여러 번 지정 가능)"),
                       days: int = Query(7, ge=1, le=30),
                       limit: int = Query(50, ge=1, le=MAX_LIMIT)) -> dict:
    _allow(request)
    response.headers["Cache-Control"] = "no-store"
    modes = [m for value in (mode or []) for m in value.split(",") if m.strip()] or None
    rows = await repo.list_turn_records(modes=modes, days=days, limit=limit)
    logger.info("internal turn-records viewed modes=%s days=%s count=%s", modes, days, len(rows))
    return {"days": days, "modes": modes, "count": len(rows), "records": [
        {"turn_id": r["turn_id"], "created_at": r["created_at"].isoformat(), "response_mode": r["response_mode"],
         "transport": r["transport"], "release_id": r["release_id"], "question": r["question"],
         "answer": r["answer"], "products": _product_names(r["products"])}
        for r in rows]}


def _product_names(raw) -> list[str]:
    items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return [p.get("product_name") for p in items if isinstance(p, dict) and p.get("product_name")]
