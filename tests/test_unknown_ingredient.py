"""생성 문장의 확인되지 않은 성분명(그래프에 없거나 보여 주는 제품에 없는 성분)."""

from app.schemas.recommend import IngredientResult, ProductResult
from app.services.response_integrity import find_response_integrity_issues

INGREDIENTS = [IngredientResult(name="CERAMIDE NP", kor_name="세라마이드엔피")]
PRODUCTS = [ProductResult(product_id="p1", product_name="마데카 멜라 캡처 앰플", brand="센텔리안24",
                          category="앰플", matched_count=1, matched_ingredients=["CERAMIDE NP"])]
INVENTORY = {"p1": [{"name": "CERAMIDE NP", "kor_name": "세라마이드엔피"},
                    {"name": "MADECASSOSIDE", "kor_name": "마데카소사이드"}]}
VOCAB = frozenset({"ceramidenp", "세라마이드엔피", "madecassoside", "마데카소사이드", "retinol", "레티놀"})


def codes(text, inventory=INVENTORY):
    return [code for code, _ in find_response_integrity_issues(text, INGREDIENTS, PRODUCTS, inventory, VOCAB)]


def test_known_names_pass():
    assert codes("*세라마이드엔피* (CERAMIDE NP)가 장벽을 돕습니다.") == []
    assert codes("**마데카소사이드 (MADECASSOSIDE)**가 들어 있어요.") == []  # 카드 성분이 아니어도 제품 전성분에 있다
    assert codes("*세라마이드*가 들어 있어요.") == []  # 확인된 성분명의 일부
    assert codes("**센텔리안24 마데카 멜라 캡처 앰플**은 앰플이에요.\n**추천**") == []  # 제품명·제목


def test_fabricated_inci_label_is_caught():
    assert codes("**마데카솔 (MADECASSOL)**이 포함돼 있어요.") == ["UNKNOWN_INGREDIENT"]


def test_fabricated_korean_name_with_ingredient_suffix_is_caught():
    assert codes("**마데카솔론**이 있어 진정에 좋아요.") == ["UNKNOWN_INGREDIENT"]


def test_graph_ingredient_missing_from_shown_products_is_caught():
    assert codes("*레티놀*이 주름에 좋아요.") == ["UNKNOWN_INGREDIENT"]


def test_check_is_skipped_without_inventory():
    assert codes("**마데카솔 (MADECASSOL)**이 포함돼 있어요.", inventory={}) == []
