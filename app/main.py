"""Open WebUI ↔ Claude Code Bridge.

FastAPI-сервер, выступающий OpenAI-совместимым прокси
для Claude Agent SDK с per-user воркспейсами.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import openai_compat, workspaces

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Claude Code Proxy",
    description="OpenAI-compatible API bridge to Claude Agent SDK",
    version="0.1.0",
)

# CORS — Open WebUI может работать с другого порта
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Маршруты
app.include_router(openai_compat.router)
app.include_router(workspaces.router)


@app.get("/health")
async def health():
    return {"status": "ok", "workspaces_root": str(settings.workspaces_root)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
    )
