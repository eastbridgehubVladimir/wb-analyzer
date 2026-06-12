"""
Подключения к базам данных.
- PostgreSQL через SQLAlchemy (async) — оперативные данные
- ClickHouse через clickhouse-connect — аналитика
- Redis — кэш и очереди
"""
from contextlib import asynccontextmanager

import clickhouse_connect
import redis.asyncio as aioredis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.config import settings

# ── PostgreSQL / SQLAlchemy Async ──────────────────────────────────────
engine_url = make_url(settings.database_url)
engine_kwargs = {
    "echo": False,  # True — выводить SQL запросы в лог (удобно при отладке)
}
if not engine_url.drivername.startswith("sqlite"):
    engine_kwargs.update(pool_size=10, max_overflow=20)

engine = create_async_engine(settings.database_url, **engine_kwargs)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Базовый класс для всех SQLAlchemy моделей."""
    pass


@asynccontextmanager
async def get_pg_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# ── ClickHouse ──────────────────────────────────────────────
def get_clickhouse_client():
    """Синхронный клиент ClickHouse (подходит для большинства запросов)."""
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        database=settings.clickhouse_db,
    )


# ── Redis ────────────────────────────────────────────────────
redis_client: aioredis.Redis | None = None


async def init_redis():
    global redis_client
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)


async def close_redis():
    if redis_client:
        await redis_client.aclose()


def get_redis() -> aioredis.Redis:
    return redis_client
