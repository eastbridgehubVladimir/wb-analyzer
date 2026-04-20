"""
Точка входа FastAPI приложения.
Запуск: uvicorn main:app --reload
Документация: http://localhost:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.v1.router import api_router
from core.config import settings
from core.database import close_redis, init_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск: подключаем Redis
    await init_redis()
    yield
    # Остановка: закрываем соединение
    await close_redis()


app = FastAPI(
    title="WB SaaS — AI платформа для e-commerce",
    description="Аналитика, ценообразование и рекомендации для Wildberries",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
