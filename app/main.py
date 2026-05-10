from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.exception_handler import global_exception_handler
from app.api import health, sessions, profile, recommend

_STATIC_DIR = Path(__file__).parent / "static"

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
app.include_router(recommend.router)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
