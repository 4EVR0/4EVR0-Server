from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.exception_handler import global_exception_handler
from app.api import health, sessions, profile, recommend
from app.repositories.conversation_repository import ensure_table, ensure_turns_table

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_table()
    await ensure_turns_table()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_exception_handler(Exception, global_exception_handler)

app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(profile.router)
app.include_router(recommend.router)
