"""
Анализ спроса: тренды продаж, сезонность, прогноз.
Читает из ClickHouse агрегированные данные.
"""
from datetime import date, timedelta

from core.database import get_clickhouse_client
from schemas.analytics import DailyMetrics


class DemandAnalyzer:
    def get_daily_metrics(self, wb_sku: int, days: int = 30) -> list[DailyMetrics]:
        """Метрики за последние N дней из ClickHouse."""
        client = get_clickhouse_client()
        since = date.today() - timedelta(days=days)

        rows = client.query(
            """
            SELECT
                metric_date,
                wb_sku,
                sum(views)          AS views,
                sum(cart_adds)      AS cart_adds,
                sum(orders)         AS orders,
                sum(returns)        AS returns,
                sum(revenue)        AS revenue,
                avg(avg_price)      AS avg_price,
                if(sum(views) > 0, sum(orders) / sum(views), 0) AS conversion_rate
            FROM wb_analytics.daily_product_metrics
            WHERE wb_sku = {sku:UInt64}
              AND metric_date >= {since:Date}
            GROUP BY metric_date, wb_sku
            ORDER BY metric_date
            """,
            parameters={"sku": wb_sku, "since": since},
        )

        return [
            DailyMetrics(
                metric_date=row[0],
                wb_sku=row[1],
                views=int(row[2]),
                cart_adds=int(row[3]),
                orders=int(row[4]),
                returns=int(row[5]),
                revenue=float(row[6]),
                avg_price=float(row[7]),
                conversion_rate=float(row[8]),
            )
            for row in rows.result_rows
        ]

    def get_trending_skus(self, category: str, limit: int = 20) -> list[int]:
        """Топ растущих товаров в категории за последние 7 дней."""
        client = get_clickhouse_client()
        rows = client.query(
            """
            SELECT
                wb_sku,
                sum(orders) AS total_orders
            FROM wb_analytics.daily_product_metrics
            WHERE category = {cat:String}
              AND metric_date >= today() - 7
            GROUP BY wb_sku
            ORDER BY total_orders DESC
            LIMIT {limit:UInt32}
            """,
            parameters={"cat": category, "limit": limit},
        )
        return [row[0] for row in rows.result_rows]


demand_analyzer = DemandAnalyzer()
