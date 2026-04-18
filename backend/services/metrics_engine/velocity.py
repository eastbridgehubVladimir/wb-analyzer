"""
Скорость продаж (sales_velocity).

Источник данных: ClickHouse → wb_analytics.daily_product_metrics
  - По категории: суммирует orders всех SKU по дням
  - По SKU: берёт orders конкретного артикула по дням

Метрики считаются по фактическим дням с ненулевыми продажами
(дни без данных не занижают среднее).
"""
from datetime import date, timedelta

from core.database import get_clickhouse_client
from services.metrics_engine.base import SalesVelocity


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


class SalesVelocityCalculator:
    def for_category(self, category: str, days: int = 30) -> SalesVelocity:
        """Скорость продаж по всей категории (агрегат по дням)."""
        client = get_clickhouse_client()
        since = date.today() - timedelta(days=days)

        rows = client.query(
            """
            SELECT
                metric_date,
                sum(orders) AS daily_orders
            FROM wb_analytics.daily_product_metrics
            WHERE category = {cat:String}
              AND metric_date >= {since:Date}
            GROUP BY metric_date
            ORDER BY metric_date
            """,
            parameters={"cat": category, "since": since},
        ).result_rows

        return self._compute(rows, days)

    def for_sku(self, wb_sku: int, days: int = 30) -> SalesVelocity:
        """Скорость продаж конкретного SKU."""
        client = get_clickhouse_client()
        since = date.today() - timedelta(days=days)

        rows = client.query(
            """
            SELECT
                metric_date,
                sum(orders) AS daily_orders
            FROM wb_analytics.daily_product_metrics
            WHERE wb_sku = {sku:UInt64}
              AND metric_date >= {since:Date}
            GROUP BY metric_date
            ORDER BY metric_date
            """,
            parameters={"sku": wb_sku, "since": since},
        ).result_rows

        return self._compute(rows, days)

    def _compute(self, rows: list, days: int) -> SalesVelocity:
        if not rows:
            return SalesVelocity(
                period_days=days,
                avg_orders_per_day=0.0,
                peak_orders_per_day=0.0,
                median_orders_per_day=0.0,
                total_orders=0,
            )

        daily = [float(row[1]) for row in rows]
        total = int(sum(daily))
        avg = total / days  # делим на полный период, не только на дни с продажами

        return SalesVelocity(
            period_days=days,
            avg_orders_per_day=round(avg, 2),
            peak_orders_per_day=round(max(daily), 2),
            median_orders_per_day=round(_median(daily), 2),
            total_orders=total,
        )


sales_velocity_calculator = SalesVelocityCalculator()
