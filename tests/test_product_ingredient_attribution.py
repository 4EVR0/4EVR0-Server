"""제품-성분 연결 오류와 첫 추천의 민감 피부 단정."""

from app.schemas.recommend import ProductResult
from app.services.recommend_service import _UNSUPPORTED_SAFETY, _ingredient_section
from app.services.response_integrity import find_response_integrity_issues


def product(pid, name):
    return ProductResult(product_id=pid, product_name=name, brand="", category="크림", matched_count=0, matched_ingredients=[])


PRODUCTS = [product("s", "넘버즈인 4번 세라 필 깐달걀 세럼 소용량"), product("e", "AHC 텐 레볼루션 리얼 아이크림 포 페이스"),
            product("u", "유세린 우레아 리페어 크림")]
INVENTORY = {
    "s": [{"name": "UREA", "kor_name": "우레아"}, {"name": "HYALURONIC ACID", "kor_name": "하이알루로닉애씨드"},
          {"name": "CERAMIDE NP", "kor_name": "세라마이드엔피"}],
    "e": [{"name": "HYALURONIC ACID", "kor_name": "하이알루로닉애씨드"}, {"name": "LACTIC ACID", "kor_name": "락틱애씨드"}],
    "u": [{"name": "UREA", "kor_name": "우레아"}, {"name": "LACTIC ACID", "kor_name": "락틱애씨드"}],
}


def codes(text):
    return [code for code, _ in find_response_integrity_issues(text, [], PRODUCTS, INVENTORY, frozenset())]


def test_product_and_its_own_ingredients_pass():
    assert codes("**넘버즈인 4 번 세라 필 깐달걀 세럼**은 *우레아*와 *세라마이드엔피*가 들어 있어요.") == []
    assert codes("**AHC 텐 레볼루션 리얼 아이크림**은 *락틱애씨드*가 들어 있어요.") == []
    # 제품명 속 성분 단어(우레아 리페어)는 성분 연결로 보지 않는다.
    assert codes("**유세린 우레아 리페어 크림**은 *락틱애씨드*가 들어 있어요.") == []


def test_ingredient_missing_from_named_product_is_caught():
    assert codes("**AHC 텐 레볼루션 리얼 아이크림**은 *우레아*로 보습해요.") == ["MISATTRIBUTED_INGREDIENT"]


def test_two_products_in_one_sentence_need_one_holder():
    assert codes("**넘버즈인 4 번 세라 필 깐달걀 세럼**과 **AHC 텐 레볼루션 리얼 아이크림**은 *우레아*와 *락틱애씨드*가 있어요.") == []
    assert codes("**AHC 텐 레볼루션 리얼 아이크림**과 **유세린 우레아 리페어 크림**은 *세라마이드엔피*가 있어요.") == [
        "MISATTRIBUTED_INGREDIENT"]


def test_all_products_claim_must_hold_for_every_product():
    assert codes("모든 제품에는 *우레아*와 *하이알루로닉애씨드*가 공통으로 포함되어 있어요.") == ["MISATTRIBUTED_INGREDIENT"]
    # "A나 B": 제품마다 둘 중 하나만 있으면 된다.
    assert codes("모든 제품에는 *하이알루로닉애씨드*나 *락틱애씨드* 같은 성분이 들어 있어요.") == []
    assert codes("모든 제품에는 *세라마이드엔피*나 *하이알루로닉애씨드*가 들어 있어요.") == ["MISATTRIBUTED_INGREDIENT"]
    # "여러 제품에 공통"은 전부가 아니다.
    assert codes("*우레아*와 *락틱애씨드*가 여러 제품에 공통으로 있어요.") == []
    INVENTORY_ALL = {pid: rows + [{"name": "GLYCERIN", "kor_name": "글리세린"}] for pid, rows in INVENTORY.items()}
    assert [c for c, _ in find_response_integrity_issues(
        "모든 제품에 *글리세린*이 공통으로 들어 있어요.", [], PRODUCTS, INVENTORY_ALL, frozenset())] == []


def test_negative_sentence_is_not_checked():
    assert codes("**AHC 텐 레볼루션 리얼 아이크림**에는 *우레아*가 없어요.") == []


def test_first_turn_sensitive_assertion_only_in_ingredient_section():
    text = "고민 분석\n자극이 없는 제품을 찾고 계신군요.\n\n성분 설명\n- 판테놀: 진정에 도움을 줘요."
    assert not _UNSUPPORTED_SAFETY.search(_ingredient_section(text))
    text = "고민 분석\n건조함이 고민이시군요.\n\n성분 설명\n- 콜로이달오트밀: 민감한 피부에도 적합합니다."
    assert _UNSUPPORTED_SAFETY.search(_ingredient_section(text))
