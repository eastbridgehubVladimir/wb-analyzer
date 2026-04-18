"""
Запись спарсенных товаров в хранилища.

PostgreSQL → таблица products (upsert по wb_sku)
ClickHouse → таблица competitor_prices (снимок цен)

Вызывается после парсинга как побочный эффект:
данные сохраняются для накопления истории и будущей работы
metrics_engine через ClickHouse.
"""
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_clickhouse_client
from services.scraper.wb_scraper import ScrapedProduct

logger = logging.getLogger(__name__)


async def save_products_to_pg(products: list[ScrapedProduct], session: AsyncSession) -> None:
    """Upsert товаров в PostgreSQL. Конфликт по wb_sku — обновляем цену и рейтинг."""
    if not products:
        return

    # Один upsert-запрос для всего батча
    values_sql = ", ".join(
        f"({p.wb_sku!r}, {p.name!r}, {p.brand!r}, {p.price!r}, {p.rating!r})"
        for p in products
    )
    await session.execute(text(f"""
        INSERT INTO products (wb_sku, name, brand, is_active, created_at, updated_at)
        VALUES {values_sql}
        ON CONFLICT (wb_sku) DO UPDATE SET
            name       = EXCLUDED.name,
            brand      = EXCLUDED.brand,
            updated_at = NOW()
    """))

    # price_history — одна запись за каждый снимок
    price_values = ", ".join(
        f"({p.wb_sku!r}, {p.price!r}, "
        f"{p.price_with_card if p.price_with_card is not None else 'NULL'}, "
        f"{p.old_price if p.old_price is not None else 'NULL'})"
        for p in products
    )
    await session.execute(text(f"""
        INSERT INTO price_history (wb_sku, price, price_with_card, old_price)
        VALUES {price_values}
    """))

    await session.commit()
    logger.info("PG: сохранено %d товаров", len(products))


def save_prices_to_clickhouse(our_sku: int, products: list[ScrapedProduct]) -> None:
    """
    Записывает снимок цен конкурентов в ClickHouse.

    our_sku — артикул нашего товара (или первого из списка как опорная точка).
    Каждый спарсенный товар рассматривается как конкурент.
    """
    if not products:
        return

    client = get_clickhouse_client()
    now = datetime.utcnow()
    today = now.date()

    rows = [
        [
            today,       # snapshot_date
            now,         # snapshot_time
            our_sku,     # our_sku
            p.wb_sku,    # rival_sku
            p.price,     # rival_price
            p.rating,    # rival_rating
            p.reviews_count,  # rival_reviews
            0,           # rival_position (не известна без поиска)
        ]
        for p in products
        if p.price > 0
    ]

    client.insert(
        "wb_analytics.competitor_prices",
        rows,
        column_names=[
            "snapshot_date", "snapshot_time",
            "our_sku", "rival_sku",
            "rival_price", "rival_rating", "rival_reviews", "rival_position",
        ],
    )
    logger.info("ClickHouse: записано %d снимков цен", len(rows))
