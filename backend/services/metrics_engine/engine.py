"""
NicheMetricsEngine — точка входа в metrics_engine.

Компонует все шесть метрик в единый отчёт.

Использование:

  # Анализ ниши целиком
  engine = NicheMetricsEngine()
  report = engine.analyze_niche("смартфоны", days=30)

  # Оборачиваемость конкретного SKU (нужна PG-сессия)
  turnover = await engine.get_stock_turnover(wb_sku=12345678, session=db)

  # Отдельные метрики
  trend = engine.get_trend(wb_sku=12345678, days=30)
  velocity = engine.get_velocity(wb_sku=12345678, days=30)

Все методы кроме get_stock_turnover — синхронные.
get_stock_turnover — async: требует AsyncSession (обращается к PostgreSQL).
"""
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from services.metrics_engine.base import (
    DemandTrend,
    NicheReport,
    PriceDistribution,
    RevenueEstimate,
    SalesVelocity,
    StockTurnover,
)
from services.metrics_engine.competition import competition_analyzer
from services.metrics_engine.price_dist import price_distribution_analyzer
from services.metrics_engine.revenue import revenue_estimator
from services.metrics_engine.trend import demand_trend_analyzer
from services.metrics_engine.turnover import stock_turnover_calculator
from services.metrics_engine.velocity import sales_velocity_calculator


class NicheMetricsEngine:
    # ──────────────────────────────────────────────
    # Полный анализ ниши
    # ──────────────────────────────────────────────

    def analyze_niche(self, category: str, days: int = 30) -> NicheReport:
        """
        Запускает все метрики для категории и возвращает NicheReport.

        Данные берутся только из ClickHouse — вызов синхронный.
        StockTurnover в отчёт не включён (per-SKU метрика).
        """
        return NicheReport(
            category=category,
            period_days=days,
            as_of_date=date.today(),
            revenue=revenue_estimator.calculate(category, days),
            velocity=sales_velocity_calculator.for_category(category, days),
            competition=competition_analyzer.calculate(category),
            price_distribution=price_distribution_analyzer.calculate(category),
            trend=demand_trend_analyzer.for_category(category, days),
        )

    # ──────────────────────────────────────────────
    # Отдельные метрики по SKU
    # ──────────────────────────────────────────────

    def get_revenue(self, category: str, days: int = 30) -> RevenueEstimate:
        return revenue_estimator.calculate(category, days)

    def get_velocity(self, wb_sku: int, days: int = 30) -> SalesVelocity:
        return sales_velocity_calculator.for_sku(wb_sku, days)

    def get_trend(self, wb_sku: int, days: int = 30) -> DemandTrend:
        return demand_trend_analyzer.for_sku(wb_sku, days)

    def get_price_distribution(self, category: str, snapshot_days: int = 3) -> PriceDistribution:
        return price_distribution_analyzer.calculate(category, snapshot_days)

    async def get_stock_turnover(
        self,
        wb_sku: int,
        session: AsyncSession,
        days: int = 30,
    ) -> StockTurnover:
        """
        Единственный async-метод: читает stock_levels из PostgreSQL.
        Вызывать из FastAPI-роутера или ARQ-воркера с активной сессией.

        Пример в роутере:
            async def endpoint(wb_sku: int, db: AsyncSession = Depends(pg_session)):
                return await metrics_engine.get_stock_turnover(wb_sku, db)
        """
        return await stock_turnover_calculator.calculate(wb_sku, session, days)


metrics_engine = NicheMetricsEngine()
