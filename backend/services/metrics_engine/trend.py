"""
Динамика спроса (demand_trend).

Источник данных:
  - ClickHouse → daily_product_metrics (orders по дням)
      Основной сигнал тренда: линейная регрессия по заказам.
  - ClickHouse → search_queries (frequency по ключевым словам)
      Вспомогательный сигнал: изменение частоты поиска WoW.

Алгоритм тренда:
  1. Линейная регрессия orders ~ day_index → slope, r²
  2. Week-over-week: сравниваем последние 7 дней с предыдущими 7
  3. TrendDirection:
     - GROWING:   slope > 0.1 и r² > 0.3
     - DECLINING: slope < -0.1 и r² > 0.3
     - STABLE:    иначе (шум, нет явного направления)

Нет внешних зависимостей: регрессия реализована на чистом Python.
"""
from datetime import date, timedelta

from core.database import get_clickhouse_client
from services.metrics_engine.base import DemandTrend, TrendDirection

_MIN_R2_FOR_TREND = 0.3    # ниже — считаем данные шумными
_MIN_SLOPE_FOR_TREND = 0.1  # заказов в день — порог значимости


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Возвращает (slope, intercept, r_squared). Без внешних зависимостей."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_yy = sum((y - mean_y) ** 2 for y in ys)

    if ss_xx == 0:
        return 0.0, mean_y, 0.0

    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_yy > 0 else 1.0

    return slope, intercept, r_squared


def _wow_change(daily_orders: list[float]) -> float:
    """
    Week-over-week изменение в процентах.
    Сравниваем сумму последних 7 дней с суммой предыдущих 7.
    Возвращает 0.0 если данных меньше 14 дней.
    """
    if len(daily_orders) < 14:
        return 0.0
    last_week = sum(daily_orders[-7:])
    prev_week = sum(daily_orders[-14:-7])
    if prev_week == 0:
        return 0.0
    return round((last_week - prev_week) / prev_week * 100, 1)


def _classify(slope: float, r_squared: float) -> TrendDirection:
    if r_squared < _MIN_R2_FOR_TREND:
        return TrendDirection.STABLE
    if slope >= _MIN_SLOPE_FOR_TREND:
        return TrendDirection.GROWING
    if slope <= -_MIN_SLOPE_FOR_TREND:
        return TrendDirection.DECLINING
    return TrendDirection.STABLE


class DemandTrendAnalyzer:
    def for_category(self, category: str, days: int = 30) -> DemandTrend:
        """Тренд спроса по всей категории."""
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

        trend = self._compute_trend(rows, days)

        search_change = self._search_wow(client, category, since)
        trend.search_frequency_change = search_change

        return trend

    def for_sku(self, wb_sku: int, days: int = 30) -> DemandTrend:
        """Тренд спроса конкретного SKU."""
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

        trend = self._compute_trend(rows, days)
        # search_queries не привязаны к конкретному SKU (только к keyword)
        trend.search_frequency_change = None
        return trend

    def _compute_trend(self, rows: list, days: int) -> DemandTrend:
        if not rows:
            return DemandTrend(
                period_days=days,
                direction=TrendDirection.STABLE,
                slope_orders_per_day=0.0,
                pct_change_wow=0.0,
                r_squared=0.0,
                search_frequency_change=None,
            )

        daily_orders = [float(row[1]) for row in rows]
        xs = list(range(len(daily_orders)))

        slope, _, r_squared = _linear_regression(xs, daily_orders)
        wow = _wow_change(daily_orders)
        direction = _classify(slope, r_squared)

        return DemandTrend(
            period_days=days,
            direction=direction,
            slope_orders_per_day=round(slope, 4),
            pct_change_wow=wow,
            r_squared=round(r_squared, 4),
            search_frequency_change=None,  # заполнится в for_category
        )

    def _search_wow(self, client, category: str, since: date) -> float | None:
        """
        WoW изменение частоты поисковых запросов по товарам категории.
        Читает из search_queries, группируя по неделям.
        Возвращает None если данных нет.
        """
        rows = client.query(
            """
            SELECT
                query_date,
                sum(frequency) AS daily_freq
            FROM wb_analytics.search_queries AS sq
            INNER JOIN (
                SELECT DISTINCT wb_sku
                FROM wb_analytics.daily_product_metrics
                WHERE category = {cat:String}
            ) AS cat_skus ON sq.wb_sku = cat_skus.wb_sku
            WHERE sq.query_date >= {since:Date}
            GROUP BY query_date
            ORDER BY query_date
            """,
            parameters={"cat": category, "since": since},
        ).result_rows

        if not rows:
            return None

        freqs = [float(row[1]) for row in rows]
        change = _wow_change(freqs)
        return change if len(freqs) >= 14 else None


demand_trend_analyzer = DemandTrendAnalyzer()
