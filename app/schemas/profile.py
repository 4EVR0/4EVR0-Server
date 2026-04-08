from app.domain.user import UserProfile


class ProfileExtractResponse(UserProfile):
    extraction_method: str  # "llm" | "rule_based"
