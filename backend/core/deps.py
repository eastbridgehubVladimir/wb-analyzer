"""
Dependency Injection для FastAPI.
Функции ниже используются как depends= в роутерах.
"""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal, get_clickhouse_client, get_redis


async def pg_session() -> AsyncGenerator[AsyncSession, None]:
    """Выдаёт сессию PostgreSQL и гарантирует её закрытие."""
    async with AsyncSessionLocal() as session:
        yield session


def ch_client():
    """Выдаёт клиент ClickHouse."""
    return get_clickhouse_client()


def redis():
    """Выдаёт Redis клиент."""
    return get_redis()
