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


class ProductResult(BaseModel):
    product_id: str
    product_name: str
    brand: str
    category: str
    matched_count: int
    matched_ingredients: list[str]


class RecommendResponse(BaseModel):
    session_id: str
    turn_id: str
    ingredients: list[IngredientResult]
    products: list[ProductResult]
    response_text: str
    model_used: str
