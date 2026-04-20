"""
Распределение цен в нише (price_distribution).

Источник данных: ClickHouse → wb_analytics.competitor_prices
  - rival_price за последние snapshot_days дней
  - Один снимок на пару (our_sku, rival_sku) в сутки — берём последний

Все вычисления (квантили, std_dev) считаются на Python-стороне,
чтобы не зависеть от конкретной версии ClickHouse и держать логику прозрачной.
"""
import math
from datetime import date, timedelta

from core.database import get_clickhouse_client
from services.metrics_engine.base import PriceDistribution


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Линейная интерполяция квантиля для отсортированного списка."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    idx = q * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _std_dev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


class PriceDistributionAnalyzer:
    def calculate(self, category: str, snapshot_days: int = 3) -> PriceDistribution:
        """
        Берёт актуальные цены конкурентов за последние snapshot_days дней.
        Короткий период (3–7 дней) даёт свежий срез без устаревших данных.
        """
        client = get_clickhouse_client()
        since = date.today() - timedelta(days=snapshot_days)

        # Последняя цена каждого конкурента для товаров нашей категории
        rows = client.query(
            """
            SELECT cp.rival_price
            FROM wb_analytics.competitor_prices AS cp
            INNER JOIN (
                SELECT DISTINCT wb_sku
                FROM wb_analytics.daily_product_metrics
                WHERE category = {cat:String}
            ) AS cat_skus ON cp.our_sku = cat_skus.wb_sku
            WHERE cp.snapshot_date >= {since:Date}
              AND cp.rival_price > 0
            """,
            parameters={"cat": category, "since": since},
        ).result_rows

        if not rows:
            return PriceDistribution(
                sample_size=0,
                min_price=0.0,
                max_price=0.0,
                p25=0.0,
                median=0.0,
                p75=0.0,
                std_dev=0.0,
                iqr=0.0,
            )

        prices = sorted(float(row[0]) for row in rows)
        p25 = _quantile(prices, 0.25)
        median = _quantile(prices, 0.50)
        p75 = _quantile(prices, 0.75)

        return PriceDistribution(
            sample_size=len(prices),
            min_price=round(prices[0], 2),
            max_price=round(prices[-1], 2),
            p25=round(p25, 2),
            median=round(median, 2),
            p75=round(p75, 2),
            std_dev=round(_std_dev(prices), 2),
            iqr=round(p75 - p25, 2),
        )


price_distribution_analyzer = PriceDistributionAnalyzer()
