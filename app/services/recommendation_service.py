import random
import logging

import openai

from app.clients.llm_client import make_llm_client
from app.clients.graph_retriever import retrieve_ingredients
from app.core.config import settings
from app.services.user_profile_extraction_service import extract_profile
from app.services.conversation_service import get_history, add_turn

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """당신은 화장품 성분 전문가입니다.
사용자의 피부 고민에 맞는 성분을 추천하고, 과학적 근거를 함께 설명해주세요.
아래 제공된 성분 데이터를 기반으로 답변하며, 근거 논문은 PMID로 인용해주세요.
한국어로 자연스럽고 친절하게 답변하세요."""


def _select_model() -> str:
    if settings.llm_model_b and random.random() < settings.ab_test_ratio:
        return settings.llm_model_b
    return settings.llm_model


def _format_graph_context(results: list[dict]) -> str:
    if not results:
        return ""
    lines = []
    for r in results:
        pmid = f" (PMID:{r['pmid']})" if r.get("pmid") else ""
        lines.append(f"- {r['ingredient']}: {r['claim']} [{r['tier']}]{pmid}")
    return "\n".join(lines)


async def recommend(session_id: str, message: str) -> dict:
    # 1. 프로필 추출 (rule-based) → effects 목록
    profile = extract_profile(message)
    effects = [e.value for e in profile.effects]

    # 2. GraphRAG — Neo4j에서 관련 성분 검색
    graph_results = await retrieve_ingredients(effects)
    graph_context_text = _format_graph_context(graph_results)

    # 3. 대화 히스토리 로드
    history = await get_history(session_id)

    # 4. 메시지 구성
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if graph_context_text:
        messages.append({
            "role": "system",
            "content": f"관련 성분 데이터:\n{graph_context_text}",
        })
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    # 5. LLM 호출
    model = _select_model()
    client = make_llm_client()
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.3,
    )
    response_text = response.choices[0].message.content or ""

    # 6. 턴 저장
    await add_turn(session_id, "user", message, {"effects": effects})
    turn_id = await add_turn(
        session_id, "assistant", response_text,
        {"ingredients": [r["ingredient"] for r in graph_results]},
    )

    return {
        "ingredients": graph_results,
        "response_text": response_text,
        "turn_id": turn_id,
        "model_used": model,
    }
