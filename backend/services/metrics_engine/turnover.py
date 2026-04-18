"""
Оборачиваемость остатков (stock_turnover).

Источник данных:
  - PostgreSQL → stock_levels
      Текущий остаток по всем складам для wb_sku.
      Это оперативные данные — живут в PG, а не в ClickHouse.
  - ClickHouse → daily_product_metrics
      Средние продажи в день за последние 30 дней.

Формула:
  days_of_stock = total_stock / avg_daily_orders

  is_at_risk = days_of_stock < 14  (риск out-of-stock в течение 2 недель)

Метод async: требует AsyncSession для PostgreSQL.
"""
from datetime import date, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_clickhouse_client
from models.pg.product import StockLevel
from services.metrics_engine.base import StockTurnover


class StockTurnoverCalculator:
    OUT_OF_STOCK_RISK_DAYS = 14

    async def calculate(self, wb_sku: int, session: AsyncSession, days: int = 30) -> StockTurnover:
        total_stock = await self._get_total_stock(wb_sku, session)
        avg_daily_orders = self._get_avg_daily_orders(wb_sku, days)

        if avg_daily_orders > 0:
            days_of_stock = round(total_stock / avg_daily_orders, 1)
        else:
            days_of_stock = None  # нет данных о продажах — нельзя считать оборачиваемость

        return StockTurnover(
            wb_sku=wb_sku,
            total_stock=total_stock,
            avg_daily_orders=round(avg_daily_orders, 2),
            days_of_stock=days_of_stock,
            is_at_risk=(
                days_of_stock is not None
                and days_of_stock < self.OUT_OF_STOCK_RISK_DAYS
            ),
        )

    async def _get_total_stock(self, wb_sku: int, session: AsyncSession) -> int:
        """Суммарный остаток по всем складам из PostgreSQL."""
        result = await session.execute(
            select(func.coalesce(func.sum(StockLevel.quantity), 0)).where(
                StockLevel.wb_sku == wb_sku
            )
        )
        return int(result.scalar())

    def _get_avg_daily_orders(self, wb_sku: int, days: int) -> float:
        """Средние заказы в день из ClickHouse за последние N дней."""
        client = get_clickhouse_client()
        since = date.today() - timedelta(days=days)

        rows = client.query(
            """
            SELECT sum(orders) AS total_orders
            FROM wb_analytics.daily_product_metrics
            WHERE wb_sku = {sku:UInt64}
              AND metric_date >= {since:Date}
            """,
            parameters={"sku": wb_sku, "since": since},
        ).result_rows

        if not rows or rows[0][0] is None:
            return 0.0

        total_orders = float(rows[0][0])
        return total_orders / days


stock_turnover_calculator = StockTurnoverCalculator()
