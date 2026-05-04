from pydantic import BaseModel


class RecommendRequest(BaseModel):
    session_id: str
    message: str
    category: str | None = None


class IngredientResult(BaseModel):
    name: str
    claim: str
    eligibility_tier: str
    paper_ref: str | None = None


class RecommendResponse(BaseModel):
    session_id: str
    turn_id: int
    ingredients: list[IngredientResult]
    response_text: str
    model_used: str
