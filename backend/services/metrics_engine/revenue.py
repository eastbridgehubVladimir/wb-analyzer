"""
Оценка выручки ниши (revenue_estimate).

Источник данных: ClickHouse → wb_analytics.daily_product_metrics
  - Суммирует revenue по всем SKU категории за период
  - Считает концентрацию: доля топ-20% SKU в суммарной выручке
"""
from datetime import date, timedelta

from core.database import get_clickhouse_client
from services.metrics_engine.base import RevenueEstimate


class RevenueEstimator:
    def calculate(self, category: str, days: int = 30) -> RevenueEstimate:
        client = get_clickhouse_client()
        since = date.today() - timedelta(days=days)

        # Выручка по каждому SKU за период
        rows = client.query(
            """
            SELECT
                wb_sku,
                sum(revenue) AS sku_revenue
            FROM wb_analytics.daily_product_metrics
            WHERE category = {cat:String}
              AND metric_date >= {since:Date}
              AND revenue > 0
            GROUP BY wb_sku
            ORDER BY sku_revenue DESC
            """,
            parameters={"cat": category, "since": since},
        ).result_rows

        if not rows:
            return RevenueEstimate(
                category=category,
                period_days=days,
                total_revenue=0.0,
                monthly_estimate=0.0,
                avg_revenue_per_sku=0.0,
                top_20pct_share=0.0,
            )

        revenues = [float(row[1]) for row in rows]
        total = sum(revenues)
        sku_count = len(revenues)

        monthly_estimate = total / days * 30 if days > 0 else 0.0
        avg_per_sku = total / sku_count

        # Концентрация: доля топ-20% SKU в выручке (принцип Парето)
        top_n = max(1, sku_count // 5)
        top_20_revenue = sum(revenues[:top_n])  # список уже отсортирован DESC
        top_20pct_share = top_20_revenue / total if total > 0 else 0.0

        return RevenueEstimate(
            category=category,
            period_days=days,
            total_revenue=round(total, 2),
            monthly_estimate=round(monthly_estimate, 2),
            avg_revenue_per_sku=round(avg_per_sku, 2),
            top_20pct_share=round(top_20pct_share, 4),
        )


revenue_estimator = RevenueEstimator()
