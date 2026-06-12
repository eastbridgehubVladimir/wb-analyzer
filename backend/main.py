"""
Точка входа FastAPI приложения.
Запуск: uvicorn main:app --reload
Документация: http://localhost:8000/docs
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

logger = logging.getLogger(__name__)
from fastapi.middleware.cors import CORSMiddleware

from api.v1.router import api_router
from core.config import settings
from core.database import close_redis, init_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_redis()
    except Exception as exc:
        logger.warning("Redis недоступен, продолжаем без него: %s", exc)
    yield
    try:
        await close_redis()
    except Exception:
        pass


app = FastAPI(
    title="WB SaaS — AI платформа для e-commerce",
    description="Аналитика, ценообразование и рекомендации для Wildberries",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
