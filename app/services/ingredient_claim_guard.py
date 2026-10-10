"""Bounded ingredient/benefit guard, not a general natural-language entailment judge.

A sentence fails only when it states a benefit that none of its mentioned ingredients
supports ("A와 B가 보습과 각질 연화를 돕는다" passes if A supports one and B the other).
Superlatives and amount claims fail: the graph has paper counts, not effect size or amounts. Inventory/product titles
are not parsed as generated claims. This guards benefits, not every mechanism.
"""
import re
from app.services.ingredient_explanations import product_explanations

POLICY_VERSION = "ingredient-claim-guard-v3"
PATTERNS = {
    'tone': r'미백|색소|잡티|피부\s*톤|브라이트닝|whiten|brighten|depigment',
    'aging': r'주름|탄력|노화|리프팅|wrinkle|anti[- ]?aging',
    'hydrate': r'보습|수분|hydrat|moistur',
    'soothe': r'진정|항염|염증|sooth|anti[- ]?inflamm',
    'barrier': r'장벽|barrier',
    'exfoliate': r'각질|keratol|exfoliat',
    'antioxidant': r'항산화|antioxid',
    'sebum': r'피지|sebum',
    'antimicrobial': r'항균|antimicrob',
    'pores': r'모공\s*막힘|comedol',
    'repair': r'상처|피부\s*회복|wound',
    'uv': r'자외선(?!\s*차단제)|photoprotect',  # '자외선 차단제를 바르세요'는 사용 팁이다
    'blemish': r'트러블|여드름|blemish|acne',
}


# 논문 건수는 연구 편수일 뿐 효과의 크기·순위가 아니고, 그래프에는 함량이 없다.
UNSUPPORTED_ASSERTION = re.compile(
    r'가장\s*\S{0,6}\s*(?:강력|효과|우수|좋|뛰어)|최고|최상|최강|극대화|most\s+effective|strongest'
    r'|주성분|주력|주된[^.\n]{0,10}?성분|고농도|농도가?\s*(?:높|진하)|농축|입증|증명'
    r'|흡수가?\s*(?:빠|잘\s*되)', re.I)
# 위 표현을 부정하는 문장("가장 강력하다고 단정할 수는 없습니다")은 주장이 아니다.
HEDGE = re.compile(r'단정할\s*수\s*(?:는\s*)?없|단정하기\s*어렵|알\s*수\s*없|확인되지\s*않|입증되지\s*않|아닙니다')
_SENTENCES = re.compile(r'(?<=[.!?])\s+|\n')


def has_unsupported_assertion(text):
    return any(UNSUPPORTED_ASSERTION.search(sentence) and not HEDGE.search(sentence)
               for sentence in _SENTENCES.split(text))


def benefits(text):
    return {key for key, pattern in PATTERNS.items() if re.search(pattern, text, re.I)}


def has_ingredient_claim_violation(text, ingredients, products=()):
    # Product section is server-authored or covered by the separate inclusion guard.
    prose = re.split(r'(?m)^\s*[#*\d. ]*추천 제품', text, maxsplit=1)[0]
    if has_unsupported_assertion(prose):
        return True
    allowed = {}
    aliases = {}
    for row in ingredients:
        allowed.setdefault(row.name, set()).update(benefits(row.claim or ''))
        # 대표 효능 하나만이 아니라 근거가 있는 효능 전체를 허용한다(#49: 고민별 근거는 효능이 여러 개).
        for claim in getattr(row, 'supported_claims', None) or []:
            allowed[row.name].update(benefits(claim))
        aliases.setdefault(row.name, set()).update(a.casefold() for a in (row.name, row.kor_name) if a)
    for product in products:
        for card in product_explanations(product):
            allowed.setdefault(card.name, set()).update(benefits(card.explanation))
            aliases.setdefault(card.name, set()).update(a.casefold() for a in (card.name, card.kor_name) if a)
    # Each summary key has an explicit graph effect. It is not permission to
    # transfer another ingredient's effects to this ingredient.
    for product in products:
        for item in (getattr(product, 'concern_summary', None) or {}).get('key_ingredients', []):
            name = item['inci_name']
            allowed.setdefault(name, set()).update(benefits(str(item.get('effect_code', '')).replace('_', ' ')))
            aliases.setdefault(name, set()).update(a.casefold() for a in (name, item.get('name')) if a)
    in_section = False
    current = []
    for line in prose.splitlines():
        if '성분 설명' in line:
            in_section = True
            current = []
            continue
        folded = line.casefold()
        mentioned = [name for name, names in aliases.items() if any(
            re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', folded) for alias in names)]
        if mentioned:
            current = mentioned
        elif line.lstrip().startswith(('-', '*', '•')):
            current = []
        claims = benefits(line)
        subjects = mentioned or (current if in_section else [])
        if in_section and claims and not subjects:
            return True
        # 언급한 성분 중 누구도 근거가 없는 효능이 있을 때만 오류다.
        if subjects and not claims <= set().union(*(allowed.get(name, set()) for name in subjects)):
            return True
    return False
