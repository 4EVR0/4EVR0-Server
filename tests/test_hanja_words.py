"""생성 문장에 섞인 한자: 자주 나오는 단어는 한글로 바꾸고, 남은 한자를 지우면 깨진 문장으로 본다."""

from app.services.recommend_service import _remove_hanja


def test_common_hanja_words_become_hangul_without_breaking_sentence():
    text, removed = _remove_hanja("피부 장벽을 强化하고 진정에 도움을 줍니다. 保湿 效果가 있어요.")
    assert text == "피부 장벽을 강화하고 진정에 도움을 줍니다. 보습 효과가 있어요."
    assert removed is False


def test_remaining_hanja_is_removed_and_reported():
    text, removed = _remove_hanja("바르면 效果更好합니다.")
    assert text == "바르면 효과합니다."
    assert removed is True
