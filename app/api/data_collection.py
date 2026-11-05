"""수집·이용 목적 안내, 피드백과 내 데이터 삭제 API."""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.sessions import COOKIE_NAME, _check_origin
from app.repositories import data_collection_repository as repo
from app.services import consent_policy, data_collection as collection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["data"])
STORE_UNAVAILABLE = "지금은 요청을 처리할 수 없어요. 잠시 후 다시 시도해 주세요."


class FeedbackRequest(BaseModel):
    turn_id: str = Field(min_length=1, max_length=64)
    helpful: bool
    issues: list[str] = Field(default_factory=list, max_length=len(consent_policy.FEEDBACK_ISSUES))
    note: str | None = Field(default=None, max_length=consent_policy.NOTE_MAX_CHARS)
    collection_policy_version: str | None = Field(default=None, max_length=64)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/data-policy")
async def get_data_policy(response: Response) -> dict:
    _no_store(response)
    return {"policy_version": consent_policy.POLICY_VERSION, "retention_days": consent_policy.RETENTION_DAYS,
            "texts": consent_policy.TEXTS, "feedback_issues": consent_policy.FEEDBACK_ISSUES,
            "collection_mode": "notice", "training_use": False}


@router.api_route("/consents", methods=["GET", "PUT"], deprecated=True)
async def retired_consents(request: Request):
    if request.method == "PUT":
        _check_origin(request)
    raise HTTPException(status_code=410, detail="데이터 수집 안내가 변경됐습니다. 페이지를 새로고침해 주세요.",
                        headers={"Cache-Control": "no-store"})


@router.post("/feedback", status_code=204)
async def post_feedback(body: FeedbackRequest, request: Request, response: Response) -> Response:
    _check_origin(request)
    issues = [code for code in dict.fromkeys(body.issues) if code in consent_policy.FEEDBACK_ISSUES]
    if len(issues) != len(set(body.issues)):
        raise HTTPException(status_code=422, detail="알 수 없는 항목이 있어요.")
    if not await collection.owns_turn(body.turn_id, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=404, detail="이 답변에는 의견을 남길 수 없어요.")
    subject = collection.ensure_subject(request, response)
    try:
        note = (body.note or "").strip() or None
        if not collection.current_policy(body.collection_policy_version) or (note and not collection.safe_for_quality_storage(note)):
            note = None
        await repo.upsert_feedback(turn_id=body.turn_id, subject_id=subject, helpful=body.helpful, issues=issues,
                                   note=note,
                                   retention_days=consent_policy.RETENTION_DAYS)
    except Exception as exc:
        logger.warning("feedback write failed: %s", exc)
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    response.status_code = 204
    return response


@router.delete("/my-data")
async def delete_my_data(request: Request, response: Response) -> dict:
    """이 브라우저의 품질 개선용 대화·의견을 지운다. 이후 새 대화는 안내에 따라 수집된다."""
    _check_origin(request)
    _no_store(response)
    subject = collection.subject_from(request)
    if subject is None:
        return {"deleted": {"turn_records": 0, "feedback": 0}}
    try:
        deleted = await repo.delete_subject_data(subject)
    except Exception as exc:
        logger.warning("my-data delete failed: %s", exc)
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    return {"deleted": deleted}
