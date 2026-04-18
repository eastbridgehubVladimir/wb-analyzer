"""
Вычисление метрик напрямую из списка ScrapedProduct.

Используется в /analysis/category, когда данных в ClickHouse ещё нет —
только что спарсили и сразу считаем.

Эвристика продаж (WB):
  monthly_orders ≈ reviews_count × 0.1
  Это грубая оценка, принятая в среде WB-аналитиков.
  При наличии исторических данных в ClickHouse нужно использовать
  metrics_engine напрямую — он даст точные цифры.
"""
import math

from services.metrics_engine.base import (
    CompetitionLevel,
    CompetitionReport,
    PriceDistribution,
    RevenueEstimate,
    SalesVelocity,
)
from services.scraper.wb_scraper import ScrapedProduct

_REVIEWS_TO_MONTHLY_ORDERS = 0.1  # эвристика: заказы/мес ≈ отзывы × 0.1


# ── Вспомогательные функции ──────────────────────────────

def _quantile(sorted_vals: list[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    idx = q * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    return sorted_vals[lo] * (1 - (idx - lo)) + sorted_vals[hi] * (idx - lo)


def _std_dev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _competition_level(count: int) -> CompetitionLevel:
    if count < 10:
        return CompetitionLevel.LOW
    if count < 50:
        return CompetitionLevel.MEDIUM
    if count < 200:
        return CompetitionLevel.HIGH
    return CompetitionLevel.SATURATED


# ── Метрики ──────────────────────────────────────────────

def build_revenue_estimate(products: list[ScrapedProduct], category: str) -> RevenueEstimate:
    if not products:
        return RevenueEstimate(
            category=category, period_days=30,
            total_revenue=0.0, monthly_estimate=0.0,
            avg_revenue_per_sku=0.0, top_20pct_share=0.0,
        )

    monthly_revenues = sorted(
        [p.price * max(1, p.reviews_count) * _REVIEWS_TO_MONTHLY_ORDERS for p in products],
        reverse=True,
    )
    total = sum(monthly_revenues)
    n = len(monthly_revenues)
    top_n = max(1, n // 5)
    top_20_share = sum(monthly_revenues[:top_n]) / total if total > 0 else 0.0

    return RevenueEstimate(
        category=category,
        period_days=30,
        total_revenue=round(total, 2),
        monthly_estimate=round(total, 2),   # уже за месяц (эвристика)
        avg_revenue_per_sku=round(total / n, 2),
        top_20pct_share=round(top_20_share, 4),
    )


def build_sales_velocity(products: list[ScrapedProduct]) -> SalesVelocity:
    if not products:
        return SalesVelocity(period_days=30, avg_orders_per_day=0.0,
                             peak_orders_per_day=0.0, median_orders_per_day=0.0,
                             total_orders=0)

    # Среднемесячные заказы каждого SKU → переводим в дневные
    daily = sorted([
        max(1, p.reviews_count) * _REVIEWS_TO_MONTHLY_ORDERS / 30
        for p in products
    ])
    total_monthly = sum(d * 30 for d in daily)

    return SalesVelocity(
        period_days=30,
        avg_orders_per_day=round(sum(daily) / len(daily), 2),
        peak_orders_per_day=round(max(daily), 2),
        median_orders_per_day=round(_quantile(daily, 0.5), 2),
        total_orders=int(total_monthly),
    )


def build_competition_report(products: list[ScrapedProduct], category: str) -> CompetitionReport:
    if not products:
        return CompetitionReport(
            category=category, active_sellers=0,
            level=CompetitionLevel.LOW, avg_reviews=0.0,
            avg_rating=0.0, top_10_revenue_share=0.0,
        )

    n = len(products)
    avg_rating = sum(p.rating for p in products) / n
    avg_reviews = sum(p.reviews_count for p in products) / n

    revenues = sorted(
        [p.price * max(1, p.reviews_count) * _REVIEWS_TO_MONTHLY_ORDERS for p in products],
        reverse=True,
    )
    total = sum(revenues)
    top10_share = sum(revenues[:10]) / total if total > 0 else 0.0

    return CompetitionReport(
        category=category,
        active_sellers=n,
        level=_competition_level(n),
        avg_reviews=round(avg_reviews, 1),
        avg_rating=round(avg_rating, 2),
        top_10_revenue_share=round(top10_share, 4),
    )


def build_price_distribution(products: list[ScrapedProduct]) -> PriceDistribution:
    if not products:
        return PriceDistribution(
            sample_size=0, min_price=0.0, max_price=0.0,
            p25=0.0, median=0.0, p75=0.0, std_dev=0.0, iqr=0.0,
        )

    prices = sorted(p.price for p in products if p.price > 0)
    if not prices:
        return PriceDistribution(
            sample_size=0, min_price=0.0, max_price=0.0,
            p25=0.0, median=0.0, p75=0.0, std_dev=0.0, iqr=0.0,
        )

    p25    = _quantile(prices, 0.25)
    median = _quantile(prices, 0.50)
    p75    = _quantile(prices, 0.75)

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
