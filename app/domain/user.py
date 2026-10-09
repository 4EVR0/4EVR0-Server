from pydantic import BaseModel, Field

from app.domain.enums import SkinType, Concern, Effect, Constraint


class UserProfile(BaseModel):
    skin_types: list[SkinType] = Field(default_factory=list)
    concerns: list[Concern] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    # 특정 제품 설명 요청(#124). intent: "recommend" | "product_info", product_mention: 사용자가 쓴 제품 이름 그대로
    intent: str | None = None
    product_mention: str | None = None
