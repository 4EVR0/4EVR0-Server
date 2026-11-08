"""사이드바 '개발자에게 의견 보내기'로 들어온 의견을 최근 순으로 출력한다(개발자용, 읽기 전용).

    POSTGRES_DSN=... python -m scripts.list_developer_reports [--limit 50] [--status new]

원문에는 사용자가 적은 내용이 들어 있으니 출력 결과를 이슈·PR·채팅에 그대로 붙이지 않는다.
"""
import argparse
import asyncio

from app.core.db import close_pool
from app.repositories.data_collection_repository import list_developer_reports
from app.services.consent_policy import REPORT_KINDS


async def main(limit: int, status: str | None) -> None:
    try:
        rows = await list_developer_reports(limit, status)
    finally:
        await close_pool()
    for row in rows:
        when = row["created_at"].strftime("%Y-%m-%d %H:%M")
        turn = f" turn={row['turn_id']}" if row["turn_id"] else ""
        print(f"#{row['id']} [{REPORT_KINDS.get(row['kind'], row['kind'])}] {when} v={row['app_version'] or '-'}"
              f" status={row['status']}{turn}\n  {row['message']}\n")
    print(f"{len(rows)}건")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--status", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.status))
