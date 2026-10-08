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
    # 식약처 사용제한 원료 기준 국내 규제 상태. banned는 조회 단계에서 제외되므로 응답에는
    # conditional | restricted | none 만 온다. restricted면 kr_limit_note에 배합한도 문구.
    kr_reg_status: str | None = None
    kr_limit_note: str | None = None
    # 민감 피부 계열 요청에서 남긴 주의 성분의 안내(그래프 sensitive_caution, GraphRAG_Pipeline #49).
    sensitive_note: str | None = None


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
    # 고민별 근거 성분 개수·핵심 성분(app.services.concern_summary.build_summary). 없으면 None
    concern_summary: dict | None = None
    # 전성분에 있는 식약처 착향제 알레르기 유발 성분 25종의 한글명(app.services.fragrance_allergens)
    fragrance_allergens: list[str] = Field(default_factory=list)
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
