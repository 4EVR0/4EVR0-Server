import inspect

from app.clients import neo4j_client
from app.domain.enums import Concern, Effect
from app.services.taxonomy_normalization_service import infer_effects, normalize_concerns


def test_trouble_wording_reaches_blemish_care_last():
    concerns = normalize_concerns("피부트러블이 자꾸 올라와요")
    assert concerns == [Concern.ACNE]
    effects = infer_effects(concerns)
    # 작용 근거가 있는 효능을 먼저, 결과만 적힌 BLEMISH_CARE는 마지막
    assert effects[-1] is Effect.BLEMISH_CARE
    assert Effect.ANTI_INFLAMMATORY in effects


def test_irritated_skin_uses_soothing_not_blemish_care():
    effects = infer_effects([Concern.IRRITATED_SKIN])
    assert Effect.SOOTHING in effects
    assert Effect.BLEMISH_CARE not in effects


def test_query_ranks_blemish_care_after_other_evidence():
    source = inspect.getsource(neo4j_client.query_ingredients_by_effects)
    assert "e.effect_code = 'BLEMISH_CARE' THEN 2" in source


def test_reference_book_evidence_labels():
    from app.services.recommend_service import _evidence_label, _source_label

    assert _evidence_label("reference_book", "0") == "참고 도서 근거"
    assert _source_label("reference_book", "x") == "참고 도서(화장품 성분 사전)"
    assert _source_label("cosing_function", "x") == "성분 기능 데이터"
    assert _source_label(None, "성분 근거") == "성분 근거"
