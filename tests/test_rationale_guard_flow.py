import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from app.core.config import settings
from app.domain.enums import Concern
from app.domain.user import UserProfile
from app.services import recommend_service as service


class ClaimGuardFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_batch_stream_summary_mode_blocks_retinol_whitening(self):
        profile=UserProfile(concerns=[Concern.WRINKLES,Concern.HYPERPIGMENTATION])
        rows=[{'name':'RETINOL','kor_name':'레티놀','claim':'Anti-aging','eligibility_tier':'pubmed_evidence','paper_ref':'1'}]
        product={'product_id':'p1','product_name':'테스트 세럼','brand':'테스트','category':'세럼',
                 'matched_count':1,'matched_ingredients':['RETINOL']}
        generated='고민 분석\n미백과 주름이 고민이시군요.\n성분 설명\n- 레티놀: 주름뿐 아니라 미백 효과도 기대할 수 있습니다.'
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=generated))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=chunks()))))
        evidence={'p1':[{'inci_name':'RETINOL','kor_name':'레티놀','effect_code':'ANTI_AGING',
                         'evidence_type':'pubmed_evidence','graph_score':.4,'paper_count':1}]}
        with (patch.object(settings,'recommend_cache_enabled',False),
              patch.object(settings,'concern_summary_enabled',True),
              patch.object(settings,'dictionary_explanations_enabled',False),
              patch.object(settings,'product_image_url_mode','public'),
              patch.object(service,'_resolve_conversation_response',new=AsyncMock(return_value=None)),
              patch.object(service,'_store_turn',new=AsyncMock()),
              patch.object(service.conversation_store,'save_active',new=AsyncMock()),
              patch.object(service,'extract_with_fallback',new=AsyncMock(return_value=(profile,'llm'))),
              patch.object(service,'query_ingredients_by_effects',new=AsyncMock(return_value=rows)),
              patch.object(service,'query_product_concern_evidence',new=AsyncMock(return_value=evidence)),
              patch.object(service,'select_products',new=AsyncMock(return_value=[product])),
              patch.object(service,'_build_llm_response',new=AsyncMock(return_value=generated)),
              patch.object(service,'get_async_llm_client',return_value=client)):
            result=await service.recommend('s','미백이랑 주름 개선 둘 다 되는 세럼 추천해줘')
            frames=[f async for f in service.recommend_stream('s','미백이랑 주름 개선 둘 다 되는 세럼 추천해줘')]
        deltas=[json.loads(f.split('data: ',1)[1])['text'] for f in frames if f.startswith('event: delta\n')]
        self.assertEqual('grounding_fallback',result.response_mode)
        self.assertNotIn('미백 효과',result.response_text)
        self.assertEqual([result.response_text],deltas)
        self.assertEqual('RETINOL',result.products[0].concern_summary['key_ingredients'][0]['inci_name'])
