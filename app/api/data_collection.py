"""동의·피드백·내 데이터 API."""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.sessions import COOKIE_NAME, _check_origin
from app.repositories import data_collection_repository as repo
from app.services import consent_policy, data_collection as collection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["data"])
STORE_UNAVAILABLE = "지금은 요청을 처리할 수 없어요. 잠시 후 다시 시도해 주세요."


class ConsentRequest(BaseModel):
    quality_storage: bool
    training_use: bool = False


class FeedbackRequest(BaseModel):
    turn_id: str = Field(min_length=1, max_length=64)
    helpful: bool
    issues: list[str] = Field(default_factory=list, max_length=len(consent_policy.FEEDBACK_ISSUES))
    note: str | None = Field(default=None, max_length=consent_policy.NOTE_MAX_CHARS)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/consents")
async def get_consents(request: Request, response: Response) -> dict:
    _no_store(response)
    try:
        granted = await collection.consents(collection.subject_from(request))
    except Exception as exc:
        logger.warning("consent read failed: %s", exc)
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    return {"policy_version": consent_policy.POLICY_VERSION, "retention_days": consent_policy.RETENTION_DAYS,
            "texts": consent_policy.TEXTS, "feedback_issues": consent_policy.FEEDBACK_ISSUES,
            "decided": bool(granted), "quality_storage": bool(granted.get(collection.QUALITY)),
            "training_use": bool(granted.get(collection.TRAINING))}


@router.put("/consents")
async def put_consents(body: ConsentRequest, request: Request, response: Response) -> dict:
    _check_origin(request)
    _no_store(response)
    if body.training_use and not body.quality_storage:
        raise HTTPException(status_code=422, detail="학습 이용은 대화 저장에 동의해야 선택할 수 있어요.")
    subject = collection.ensure_subject(request, response)
    try:
        await repo.record_consents(subject, {collection.QUALITY: body.quality_storage,
                                             collection.TRAINING: body.training_use},
                                   consent_policy.POLICY_VERSION)
        if not body.training_use:
            await repo.withdraw_training_use(subject)
    except Exception as exc:
        logger.warning("consent write failed: %s", exc)
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    return {"quality_storage": body.quality_storage, "training_use": body.training_use,
            "policy_version": consent_policy.POLICY_VERSION}


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
        granted = await collection.consents(subject)
        note = (body.note or "").strip() or None
        await repo.upsert_feedback(turn_id=body.turn_id, subject_id=subject, helpful=body.helpful, issues=issues,
                                   note=note if granted.get(collection.QUALITY) else None,
                                   retention_days=consent_policy.RETENTION_DAYS)
    except Exception as exc:
        logger.warning("feedback write failed: %s", exc)
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    response.status_code = 204
    return response


@router.delete("/my-data")
async def delete_my_data(request: Request, response: Response) -> dict:
    """저장된 대화·의견을 지우고 동의를 철회한다. '새 대화'와 다른 동작이다."""
    _check_origin(request)
    _no_store(response)
    subject = collection.subject_from(request)
    if subject is None:
        return {"deleted": {"turn_records": 0, "feedback": 0}}
    try:
        await repo.record_consents(subject, {collection.QUALITY: False, collection.TRAINING: False},
                                   consent_policy.POLICY_VERSION)
        deleted = await repo.delete_subject_data(subject)
    except Exception as exc:
        logger.warning("my-data delete failed: %s", exc)
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    return {"deleted": deleted}
