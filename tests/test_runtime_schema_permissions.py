import asyncio

import pytest

from app.core.config import settings
from app.repositories import conversation_repository, data_collection_repository, turn_event_repository


@pytest.mark.parametrize('module,method', [
    (conversation_repository, 'ensure_table'),
    (data_collection_repository, 'ensure_tables'),
    (turn_event_repository, 'ensure_tables'),
])
def test_runtime_does_not_attempt_schema_changes(monkeypatch, module, method):
    monkeypatch.setattr(settings, 'db_auto_create_schema', False)

    async def forbidden_pool():
        raise AssertionError('The runtime role must not execute schema setup')

    monkeypatch.setattr(module, 'get_pool', forbidden_pool)
    asyncio.run(getattr(module, method)())
