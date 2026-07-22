"""LLM 호출로 Cypher(추후 SQL)를 생성하고, 실행 전 EXPLAIN으로 문법을 검증한다.

app.clients.llm_factory.get_async_llm_client()를 그대로 재사용한다 (복제하지
않음 — 복제하면 pg_experiment/queries.py처럼 원본과 어긋날 수 있음, 실제로
이번 작업에서 그 드리프트를 발견했음). llm_query_eval이 4EVR0-Server 안에
있어서 이 import가 무겁지 않다 (app.clients.neo4j_client/llm_factory는
pydantic-settings + neo4j + openai만 필요, FastAPI/redis/asyncpg 불필요).
"""

import json
import sys
from pathlib import Path

import openai

_APP_ROOT = Path(__file__).resolve().parent.parent  # 4EVR0-Server/
sys.path.insert(0, str(_APP_ROOT))
from app.clients.llm_factory import get_async_llm_client  # noqa: E402
from app.core.config import settings  # noqa: E402

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

get_client = get_async_llm_client  # 프로세스 싱글턴 그대로 재사용 (별도 구현 없음)


def load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


async def _call_llm(client: openai.AsyncOpenAI, system_prompt: str, user_content: str, model: str) -> tuple[dict, str]:
    """(파싱된 dict, 원본 응답 문자열)을 반환한다.

    raw를 같이 돌려주는 이유: JSON에 "cypher" 키가 없는 등 실패 케이스에서 raw가
    없으면 LLM이 정확히 뭘 뱉었는지 못 보고 "실패했다"는 사실만 남아 원인 파악이
    안 됨 (실제로 이번 실행에서 KeyError('cypher')로 실패한 케이스 2건의 원인을
    확인할 수 없었음).
    """
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=settings.gen_temperature,
        max_tokens=settings.gen_max_tokens,
        response_format={"type": "json_object"},
        # app/services/recommend_service.py, eval/run_eval.py, eval/run_response_eval.py
        # 전부 Qwen3 계열의 "생각하기" 모드를 꺼서 씀 — 안 끄면 <think> 추론이
        # max_tokens를 잡아먹어 정작 JSON 답변이 잘리는 문제가 있었음(실제로 발생).
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GenerationError(f"JSON 파싱 실패: {exc}. raw={raw[:300]!r}") from exc
    return parsed, raw


async def generate_cypher(
    question: str, client: openai.AsyncOpenAI, model: str, prompt_name: str = "cypher_generation"
) -> tuple[dict, str]:
    """질문 1건에 대해 ({"cypher": ..., "params": ...}, raw 응답 문자열)을 생성한다.

    prompt_name: "cypher_generation"(성분) 또는 "cypher_generation_products"(제품).
    """
    prompt = load_prompt(prompt_name)
    return await _call_llm(client, prompt, question, model)


def validate_cypher(cypher: str, params: dict, driver) -> None:
    """EXPLAIN으로 문법만 확인한다 (결과는 실행하지 않음). 실패 시 예외.

    driver는 실험 스크립트 자체의 동기(sync) neo4j 드라이버 — 프로덕션이 쓰는
    app.clients.neo4j_client의 비동기 드라이버 싱글턴과는 별개 커넥션이다.
    """
    with driver.session() as session:
        session.run(f"EXPLAIN {cypher}", **params)


def run_cypher(cypher: str, params: dict, driver) -> list[dict]:
    """실제로 실행해서 결과를 받는다 (EXPLAIN이 아니라 진짜 실행).

    문법 오류는 여기서 자연히 걸리고, 결과 0건도 여기서 감지할 수 있다 — EXPLAIN만
    쓰면 "문법은 맞는데 조건이 너무 빡빡해서 아무것도 안 나오는" 쿼리를 성공으로
    잘못 판정하게 된다. 프롬프트가 조회 전용(MATCH/WHERE/WITH/RETURN/ORDER BY/LIMIT)만
    쓰라고 강제하므로 실제 실행 자체는 안전하다.
    """
    with driver.session() as session:
        return session.run(cypher, **params).data()


async def generate_and_validate(
    question: str, client: openai.AsyncOpenAI, driver, model: str, max_attempts: int = 2,
    prompt_name: str = "cypher_generation",
) -> dict:
    """생성 -> 실제 실행 -> 결과 0건이면 재시도. 실패 시 에러 메시지를 덧붙여 1회만 재생성.

    반환: {"cypher": str, "params": dict, "rows": list[dict], "attempts": int, "raw": str}
    성공 시. 실패 시 GenerationError를 던진다(호출부에서 "생성 실패" 케이스로 집계) —
    에러 메시지에 마지막 raw 응답을 잘라서 포함해 원인 진단이 가능하게 한다.
    """
    last_error: Exception | None = None
    last_raw: str | None = None
    question_for_retry = question
    for attempt in range(1, max_attempts + 1):
        try:
            result, raw = await generate_cypher(question_for_retry, client, model, prompt_name)
            last_raw = raw
            if "cypher" not in result:
                raise GenerationError(f"'cypher' 키 없음. raw={raw[:300]!r}")
            cypher, params = result["cypher"], result.get("params", {})
            rows = run_cypher(cypher, params, driver)
            if not rows:
                raise GenerationError("실행은 됐지만 결과가 0건입니다 — 조건이 너무 빡빡하거나 잘못된 코드를 골랐을 수 있습니다.")
            return {"cypher": cypher, "params": params, "rows": rows, "attempts": attempt, "raw": raw}
        except Exception as exc:  # noqa: BLE001 — 실험 스크립트, 원인 불문 재시도/집계
            last_error = exc
            question_for_retry = (
                f"{question}\n\n(이전 시도가 다음 에러로 실패했습니다: {exc}. "
                "쿼리를 수정해서 다시 작성하세요.)"
            )
    raise GenerationError(
        f"{max_attempts}회 시도 후 실패: {last_error} (마지막 raw 응답: {(last_raw or '')[:300]!r})"
    ) from last_error


class GenerationError(Exception):
    pass


# ── SQL 생성 (B vs D 단계에서 채울 스텁) ─────────────────────────────────
def generate_sql(question: str, client: openai.AsyncOpenAI, model: str) -> dict:
    raise NotImplementedError("B vs D 단계 착수 시 구현")


def validate_sql(sql: str, params: dict, pg_conn) -> None:
    raise NotImplementedError("B vs D 단계 착수 시 구현")


if __name__ == "__main__":
    import argparse
    import asyncio

    from neo4j import GraphDatabase

    ap = argparse.ArgumentParser(description="generate.py 단독 테스트 — 질문 1개로 생성+EXPLAIN 검증만 확인")
    ap.add_argument("--question", default="피부가 너무 건조하고 당겨요. 수분 채워주는 성분 추천해주세요.")
    ap.add_argument("--model", default=settings.gpu_model)
    args = ap.parse_args()

    async def _main():
        client = get_client()
        driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        try:
            result = await generate_and_validate(args.question, client, driver, args.model)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            driver.close()

    asyncio.run(_main())
