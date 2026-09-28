"""Test-only SSE route; never copied into the application image."""
import asyncio
from fastapi.responses import StreamingResponse
from app.main import app


@app.get("/__smoke/stream")
async def stream():
    async def events():
        yield "data: first\n\n"
        await asyncio.sleep(1)
        yield "data: second\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")
