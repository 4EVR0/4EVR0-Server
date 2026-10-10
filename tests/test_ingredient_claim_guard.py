from app.schemas.recommend import IngredientResult
from app.services.ingredient_claim_guard import has_ingredient_claim_violation
from app.services.concern_summary import build_summary
from app.domain.enums import Concern


def retinol():
    return IngredientResult(name='RETINOL', kor_name='레티놀', claim='Anti-aging')


def test_rejects_unprovided_benefit_and_superlative():
    for text in ('레티놀은 주름과 미백에 도움이 됩니다.', '레티놀은 가장 강력한 주름 개선 성분입니다.',
                 '레티놀: 주름 관리에 도움이 됩니다.\n미백에도 도움이 됩니다.'):
        assert has_ingredient_claim_violation('성분 설명\n'+text,[retinol()])


def test_does_not_transfer_other_ingredients_effect():
    other=IngredientResult(name='ARBUTIN',kor_name='알부틴',claim='Brightening')
    assert has_ingredient_claim_violation('성분 설명\n레티놀은 미백에 도움이 됩니다.',[retinol(),other])
    assert not has_ingredient_claim_violation('성분 설명\n- 레티놀: 주름 관리\n- 알부틴: 피부 톤 개선',[retinol(),other])


def test_claim_outside_section_and_unknown_subject():
    assert has_ingredient_claim_violation('고민 분석\n레티놀은 미백에도 좋습니다.',[retinol()])
    assert has_ingredient_claim_violation('성분 설명\n- 새로운성분: 미백에 좋습니다.',[retinol()])
    assert not has_ingredient_claim_violation('고민 분석\n미백과 주름이 고민이시군요.\n성분 설명\n- 레티놀: 주름 관리',[retinol()])


def test_inventory_title_is_not_generated_claim():
    assert not has_ingredient_claim_violation('성분 설명\n레티놀: 주름 관리\n추천 제품\n- 레티놀 미백 세럼',[retinol()])


def test_fragrance_excluded_from_summary_count_and_keys():
    rows=[{'inci_name':name,'kor_name':name,'effect_code':'ANTI_INFLAMMATORY',
           'evidence_type':'pubmed_evidence','paper_count':1,'graph_score':.47}
          for name in ['LINALOOL','FARNESOL','LIMONENE']]
    assert build_summary([Concern.ACNE],rows,functional={}) is None
    rows.append({**rows[0],'inci_name':'PANTHENOL','kor_name':'판테놀'})
    summary=build_summary([Concern.ACNE],rows,functional={})
    assert summary['concerns'][0]['count']==1
    assert [x['inci_name'] for x in summary['key_ingredients']]==['PANTHENOL']


def test_sunscreen_tip_is_not_uv_claim():
    assert not has_ingredient_claim_violation('레티놀을 바른 뒤 자외선 차단제를 꼭 바르세요.', [retinol()])
    assert has_ingredient_claim_violation('레티놀이 자외선 차단 효과를 줍니다.', [retinol()])
