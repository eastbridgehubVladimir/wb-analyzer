"""
Типы результатов для metrics_engine.
Все датаклассы — чистые данные без логики расчёта.
"""
from dataclasses import dataclass
from datetime import date
from enum import Enum


class TrendDirection(str, Enum):
    GROWING = "growing"
    STABLE = "stable"
    DECLINING = "declining"


class CompetitionLevel(str, Enum):
    LOW = "low"            # < 10 активных продавцов
    MEDIUM = "medium"      # 10–50
    HIGH = "high"          # 50–200
    SATURATED = "saturated"  # > 200


@dataclass
class RevenueEstimate:
    """Оценка выручки ниши за период.

    Источник данных: ClickHouse → daily_product_metrics
    """
    category: str
    period_days: int
    total_revenue: float          # суммарная выручка за период, ₽
    monthly_estimate: float       # экстраполяция на 30 дней
    avg_revenue_per_sku: float    # средняя выручка на один SKU
    top_20pct_share: float        # доля топ-20% SKU в выручке (0..1), концентрация рынка


@dataclass
class SalesVelocity:
    """Скорость продаж: сколько заказов в день.

    Источник данных:
      - по категории: ClickHouse → daily_product_metrics (суммарно по category)
      - по SKU: ClickHouse → daily_product_metrics (фильтр по wb_sku)
    """
    period_days: int
    avg_orders_per_day: float     # среднее за период
    peak_orders_per_day: float    # максимальный день
    median_orders_per_day: float  # медиана (устойчивее среднего)
    total_orders: int             # итого заказов за период


@dataclass
class CompetitionReport:
    """Уровень конкуренции в нише.

    Источник данных:
      - ClickHouse → competitor_prices (количество уникальных rival_sku, метрики)
      - PostgreSQL → competitors (коэффициент схожести товаров)
    """
    category: str
    active_sellers: int           # уникальных конкурентов за последние 7 дней
    level: CompetitionLevel
    avg_reviews: float            # среднее количество отзывов у конкурентов
    avg_rating: float             # средний рейтинг конкурентов
    top_10_revenue_share: float   # доля топ-10 SKU в суммарных заказах (0..1)


@dataclass
class PriceDistribution:
    """Распределение цен в нише.

    Источник данных: ClickHouse → competitor_prices (rival_price)
    """
    sample_size: int              # количество ценовых точек в выборке
    min_price: float
    max_price: float
    p25: float                    # нижний квартиль
    median: float
    p75: float                    # верхний квартиль
    std_dev: float                # стандартное отклонение
    iqr: float                    # межквартильный размах = p75 - p25


@dataclass
class StockTurnover:
    """Оборачиваемость остатков конкретного SKU.

    Источник данных:
      - PostgreSQL → stock_levels (текущий остаток по складам)
      - ClickHouse → daily_product_metrics (средние продажи в день)
    """
    wb_sku: int
    total_stock: int              # суммарный остаток по всем складам
    avg_daily_orders: float       # среднее заказов в день за последние 30 дней
    days_of_stock: float | None   # остаток / продажи_в_день; None если продаж нет
    is_at_risk: bool              # days_of_stock < 14 (риск out-of-stock)


@dataclass
class DemandTrend:
    """Динамика спроса за период.

    Источник данных:
      - ClickHouse → daily_product_metrics (orders по дням)
      - ClickHouse → search_queries (частота поисковых запросов)
    """
    period_days: int
    direction: TrendDirection
    slope_orders_per_day: float   # прирост заказов в день (линейная регрессия)
    pct_change_wow: float         # изменение заказов, последняя неделя vs предыдущая, %
    r_squared: float              # качество тренда (0..1); < 0.3 — нет тренда, шум
    search_frequency_change: float | None  # изменение частоты поиска WoW, %; None если нет данных


@dataclass
class NicheReport:
    """Полный анализ ниши — результат NicheMetricsEngine.analyze_niche()."""
    category: str
    period_days: int
    as_of_date: date
    revenue: RevenueEstimate
    velocity: SalesVelocity
    competition: CompetitionReport
    price_distribution: PriceDistribution
    trend: DemandTrend
    # StockTurnover не включён: он per-SKU, не per-category
    # Получить через engine.get_stock_turnover(wb_sku, session)
