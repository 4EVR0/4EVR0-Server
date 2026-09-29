from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    session_id: str | None = None
    message: str
    category: str | None = None


class IngredientResult(BaseModel):
    name: str
    kor_name: str | None = None
    claim: str | None = None
    eligibility_tier: str | None = None
    paper_ref: str | None = None


class ProductIngredientExplanation(BaseModel):
    """순위 산정용 매칭 성분과 구분한, 제품에서 확인된 성분의 검토 설명."""
    name: str
    kor_name: str
    explanation: str
    source_id: str
    source_page: int
    card_version: str


class ProductResult(BaseModel):
    product_id: str
    goods_no: str | None = None
    product_name: str
    brand: str
    category: str
    image_url: str | None = None
    product_url: str | None = None  # 올리브영 상품 상세페이지 링크
    matched_count: int
    matched_ingredients: list[str]
    ingredient_explanations: list[ProductIngredientExplanation] = Field(default_factory=list)
    fragrance_free_source_url: str | None = None
    # 리뷰(부연): 논문 근거가 메인, 리뷰는 사용자 합의 보조 신호
    rating: float | None = None
    review_count: int | None = None
    review_stats: dict | None = None  # {"피부고민": {...}, "자극도": {...}, ...}


class RecommendResponse(BaseModel):
    session_id: str
    turn_id: str
    ingredients: list[IngredientResult]
    products: list[ProductResult]
    response_text: str
    model_used: str
    response_mode: str = "generated"


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
