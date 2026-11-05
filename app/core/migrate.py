"""Run additive schema preparation with the dedicated migration DSN.

Invoke as `python -m app.core.migrate` before starting the production app.
Runtime containers should set DB_AUTO_CREATE_SCHEMA=false and use a DML role.
"""
import asyncio

from app.core.config import settings
from app.core.db import close_pool
from app.repositories import conversation_repository, data_collection_repository, turn_event_repository


async def migrate() -> None:
    settings.db_auto_create_schema = True
    try:
        await conversation_repository.ensure_table()
        await data_collection_repository.ensure_tables()
        await turn_event_repository.ensure_tables()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(migrate())
