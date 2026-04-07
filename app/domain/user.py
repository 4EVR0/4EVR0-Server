from pydantic import BaseModel, Field

from app.domain.enums import SkinType, Concern, Effect, Constraint


class UserProfile(BaseModel):
    skin_types: list[SkinType] = Field(default_factory=list)
    concerns: list[Concern] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
