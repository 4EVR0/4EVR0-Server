import logging

from app.clients.llm_client import call_llm
from app.domain.user import UserProfile
from app.services.user_profile_extraction_service import extract_profile

logger = logging.getLogger(__name__)


async def extract_with_fallback(message: str) -> tuple[UserProfile, str]:
    """LLM 호출을 시도하고, 타임아웃/파싱 실패 시 규칙 기반으로 폴백한다.

    Returns:
        (UserProfile, extraction_method)
        extraction_method: "llm" | "rule_based"
    """
    try:
        profile = await call_llm(message)
        return profile, "llm"
    except Exception as exc:
        logger.warning("LLM extraction failed (%s: %s), falling back to rule-based.", type(exc).__name__, exc)
        return extract_profile(message), "rule_based"
