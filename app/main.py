from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.exception_handler import global_exception_handler
from app.api import health, sessions, profile

setup_logging()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
)

app.add_exception_handler(Exception, global_exception_handler)

app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(profile.router)
