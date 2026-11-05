"""Request-local exclusions, separate from the question's concerns and ranking.

Use existing graph cautions without relaxing them for a positive acne effect.
CONTAINS is mapped inventory, not a verified complete package ingredient list;
passing this filter must never be described as a sensitive-skin safety guarantee.
"""
import hashlib
import json
from contextvars import ContextVar

from app.clients.neo4j_client import query_product_ingredient_inventory
from app.schemas.recommend import SkinProfile

current: ContextVar[SkinProfile | None] = ContextVar('skin_profile_exclusions', default=None)


def cache_identity(message: str) -> str:
    profile = current.get()
    data = {'skin': profile.skin, 'concerns': sorted(set(profile.concerns))} if profile else {}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f'skin-exclusions-v2|{digest}|{message}'


async def filter_products(products: list[dict]) -> list[dict]:
    profile = current.get()
    if not profile or not profile.sensitive or not products:
        return products
    inventory = await query_product_ingredient_inventory([p['product_id'] for p in products])
    kept = []
    for product in products:
        rows = inventory.get(product['product_id'])
        # Missing data or an unavailable graph cannot establish eligibility.
        if not rows:
            continue
        if any(row.get('sensitive_caution') in {'exclude', 'caution'}
               or row.get('sensitivity_caution') for row in rows):
            continue
        name = product.get('product_name') or product.get('name') or ''
        if '필링' in name or 'peel' in name.lower():
            continue
        kept.append(product)
    return kept


def note() -> str | None:
    profile = current.get()
    if profile and profile.sensitive:
        return ('피부 프로필을 고려해 확인된 성분에 민감 피부 주의 근거가 있는 제품은 제외했어요. '
                '성분 정보만으로 개인별 적합성을 보장할 수는 없으니 제품 전성분도 확인해 주세요.')
    return None
