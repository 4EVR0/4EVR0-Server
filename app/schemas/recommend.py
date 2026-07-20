from pydantic import BaseModel


class RecommendRequest(BaseModel):
    session_id: str
    message: str
    category: str | None = None


class IngredientResult(BaseModel):
    name: str
    kor_name: str | None = None
    claim: str | None = None
    eligibility_tier: str | None = None
    paper_ref: str | None = None


class ProductResult(BaseModel):
    product_id: str
    goods_no: str | None = None
    product_name: str
    brand: str
    category: str
    image_url: str | None = None
    matched_count: int
    matched_ingredients: list[str]


class RecommendResponse(BaseModel):
    session_id: str
    turn_id: str
    ingredients: list[IngredientResult]
    products: list[ProductResult]
    response_text: str
    model_used: str


class PathStep(BaseModel):
    node: str | None = None
    label: str | None = None
    name: str | None = None
    kor_name: str | None = None
    brand: str | None = None
    rel: str | None = None
    evidence_type: str | None = None
    graph_score: float | None = None


class PathResult(BaseModel):
    path: list[PathStep]


class PathResponse(BaseModel):
    effects: list[str]
    paths: list[PathResult]
