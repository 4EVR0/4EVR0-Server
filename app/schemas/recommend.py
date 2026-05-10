from pydantic import BaseModel


class RecommendRequest(BaseModel):
    session_id: str
    message: str
    category: str | None = None


class IngredientResult(BaseModel):
    name: str
    claim: str | None = None
    eligibility_tier: str | None = None
    paper_ref: str | None = None


class RecommendResponse(BaseModel):
    session_id: str
    turn_id: str
    ingredients: list[IngredientResult]
    response_text: str
    model_used: str
