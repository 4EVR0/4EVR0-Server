import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.domain.enums import Concern
from app.schemas.recommend import SkinProfile, IngredientResult
from app.services import skin_profile_policy as policy, recommend_service as service


class SkinProfilePolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = policy.current.set(SkinProfile(concerns=['민감함']))

    async def asyncTearDown(self):
        policy.current.reset(self.token)

    async def test_filters_full_inventory_not_only_positive_matches(self):
        products = [{'product_id': p, 'product_name': p, 'matched_ingredients': ['NIACINAMIDE']}
                    for p in ['good', 'acid', 'retinoid', 'caution', 'unknown']]
        inventory = {'good': [{'name': 'NIACINAMIDE'}],
                     'acid': [{'name': 'NIACINAMIDE'}, {'name': 'SALICYLIC ACID', 'sensitive_caution': 'exclude'}],
                     'retinoid': [{'name': 'RETINAL', 'sensitive_caution': 'caution'}],
                     'caution': [{'name': 'OTHER', 'sensitivity_caution': True}]}
        with patch.object(policy, 'query_product_ingredient_inventory', AsyncMock(return_value=inventory)):
            self.assertEqual([products[0]], await policy.filter_products(products))

    async def test_sensitive_profile_does_not_relax_for_acne_and_refills_before_limit(self):
        rows = [dict(product_id=str(i), product_name='세럼'+str(i), category='세럼',
                     matched_count=2, matched_ingredients=['NIACINAMIDE', 'PANTHENOL'], relevance_score=1)
                for i in range(8)]
        inventory = {p['product_id']: [{'name': 'PANTHENOL'}] for p in rows}
        for i in range(6):
            inventory[str(i)].append({'name': 'SALICYLIC ACID', 'sensitive_caution': 'exclude',
                                     'sensitive_caution_with': ['ACNE']})
        with (patch.object(service, 'query_products_by_ingredients', AsyncMock(return_value=rows)),
              patch.object(policy, 'query_product_ingredient_inventory', AsyncMock(return_value=inventory))):
            result = await service.select_products('여드름 세럼 추천', [Concern.ACNE], [{'name': 'NIACINAMIDE'}])
        self.assertEqual(['6', '7'], [p['product_id'] for p in result])

    async def test_non_sensitive_profile_never_adds_sebum_to_soothing_goal(self):
        policy.current.set(SkinProfile(skin='지성', concerns=['피지']))
        query = AsyncMock(return_value=[])
        with patch.object(service, 'query_products_by_ingredients', query):
            await service.select_products('진정 세럼 추천', [Concern.IRRITATED_SKIN], [{'name': 'PANTHENOL'}])
        self.assertEqual([{'name': 'PANTHENOL'}], query.call_args.args[0])
        self.assertNotIn('피지', str(query.call_args))

    async def test_inventory_failure_does_not_restore_excluded_candidates(self):
        with patch.object(policy, 'query_product_ingredient_inventory', AsyncMock(return_value={})):
            self.assertEqual([], await policy.filter_products([{'product_id': 'p'}]))

    async def test_excluded_ingredients_are_not_recommended_in_empty_or_nonempty_response(self):
        ingredients = [IngredientResult(name='SALICYLIC ACID'), IngredientResult(name='NIACINAMIDE')]
        with patch.object(service.settings, 'ingredient_selection_enabled', False):
            self.assertEqual([], service._finalize_ingredients([], [], ingredients))
            allowed = service._finalize_ingredients([], [{'matched_ingredients': ['NIACINAMIDE']}], ingredients)
            self.assertEqual(['NIACINAMIDE'], [item.name for item in allowed])
        text = service._build_no_product_response(ingredients, [], [Concern.ACNE], '여드름 추천')
        self.assertNotIn('SALICYLIC ACID', text)
        self.assertIn('제외 조건', text)

    async def test_cache_isolated_by_profile_and_order_is_canonical(self):
        a = policy.cache_identity('여드름 세럼')
        policy.current.set(SkinProfile(concerns=['피지']))
        b = policy.cache_identity('여드름 세럼')
        self.assertNotEqual(a, b)
        policy.current.set(SkinProfile(concerns=['피지', '민감함']))
        c = policy.cache_identity('여드름 세럼')
        policy.current.set(SkinProfile(concerns=['민감함', '피지']))
        self.assertEqual(c, policy.cache_identity('여드름 세럼'))

    async def test_request_context_isolated_and_reset_for_batch_and_stream(self):
        policy.current.set(None)
        message = '진정 제품 추천해줘'
        profiles = [SkinProfile(concerns=['피지']), SkinProfile(concerns=['민감함'])]
        async def batch(sid, text, prompt):
            await asyncio.sleep(0)
            self.assertEqual(message, text)
            return policy.current.get()
        async def stream(sid, text, prompt):
            self.assertEqual(message, text)
            yield policy.current.get()
        with (patch.object(service, '_recommend_with_metrics', batch),
              patch.object(service, '_recommend_stream_with_metrics', stream)):
            result = await asyncio.gather(*(service.recommend('s', message, skin_profile=p) for p in profiles))
            self.assertEqual(profiles, result)
            self.assertIsNone(policy.current.get())
            for p in profiles:
                self.assertEqual([p], [x async for x in service.recommend_stream('s', message, skin_profile=p)])
                self.assertIsNone(policy.current.get())

    async def test_followup_cannot_restore_excluded_products(self):
        active = {'profile': {'concerns': ['ACNE']}, 'visible_products': [],
                  'source_products': [{'product_id': 'acid', 'name': '각질 세럼', 'category': '세럼',
                                       'matched_ingredients': ['SALICYLIC ACID']}], 'ingredients': []}
        inventory = {'acid': [{'name': 'SALICYLIC ACID', 'sensitive_caution': 'exclude'}]}
        with (patch.object(policy, 'query_product_ingredient_inventory', AsyncMock(return_value=inventory)),
              patch.object(service, '_store_turn', AsyncMock()),
              patch.object(service.conversation_store, 'save_active', AsyncMock())):
            result = await service._handle_followup('s', 't', '전체 제품 다시 보여줘', [], active)
        self.assertEqual([], result.products)
