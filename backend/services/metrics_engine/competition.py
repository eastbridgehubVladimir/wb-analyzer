"""
Уровень конкуренции в нише (competition_level).

Источник данных:
  - ClickHouse → wb_analytics.competitor_prices
      rival_sku, rival_rating, rival_reviews — за последние 7 дней
  - ClickHouse → wb_analytics.daily_product_metrics
      концентрация заказов: доля топ-10 SKU

Порог уровней конкуренции (active_sellers):
  LOW        < 10
  MEDIUM     10–50
  HIGH       50–200
  SATURATED  > 200
"""
from datetime import date, timedelta

from core.database import get_clickhouse_client
from services.metrics_engine.base import CompetitionLevel, CompetitionReport

_THRESHOLDS: list[tuple[int, CompetitionLevel]] = [
    (10, CompetitionLevel.LOW),
    (50, CompetitionLevel.MEDIUM),
    (200, CompetitionLevel.HIGH),
]


def _classify(active_sellers: int) -> CompetitionLevel:
    for threshold, level in _THRESHOLDS:
        if active_sellers < threshold:
            return level
    return CompetitionLevel.SATURATED


class CompetitionAnalyzer:
    def calculate(self, category: str, days: int = 7) -> CompetitionReport:
        """
        Анализирует конкуренцию по данным снимков цен конкурентов.

        Параметр days лучше держать небольшим (7–14):
        competitor_prices — частые снимки, старые данные теряют смысл.
        """
        client = get_clickhouse_client()
        since = date.today() - timedelta(days=days)

        # Уникальные конкуренты + их средние метрики
        rival_rows = client.query(
            """
            SELECT
                count(DISTINCT rival_sku)    AS active_sellers,
                avg(rival_rating)            AS avg_rating,
                avg(rival_reviews)           AS avg_reviews
            FROM wb_analytics.competitor_prices AS cp
            INNER JOIN wb_analytics.daily_product_metrics AS dm
                ON cp.our_sku = dm.wb_sku
            WHERE dm.category = {cat:String}
              AND cp.snapshot_date >= {since:Date}
            """,
            parameters={"cat": category, "since": since},
        ).result_rows

        active_sellers = 0
        avg_rating = 0.0
        avg_reviews = 0.0
        if rival_rows and rival_rows[0][0]:
            active_sellers = int(rival_rows[0][0])
            avg_rating = round(float(rival_rows[0][1] or 0), 2)
            avg_reviews = round(float(rival_rows[0][2] or 0), 1)

        # Концентрация заказов: доля топ-10 SKU
        top10_share = self._top10_share(client, category, since)

        return CompetitionReport(
            category=category,
            active_sellers=active_sellers,
            level=_classify(active_sellers),
            avg_reviews=avg_reviews,
            avg_rating=avg_rating,
            top_10_revenue_share=top10_share,
        )

    def _top10_share(self, client, category: str, since: date) -> float:
        """Доля топ-10 SKU в суммарных заказах категории."""
        rows = client.query(
            """
            SELECT
                wb_sku,
                sum(orders) AS total_orders
            FROM wb_analytics.daily_product_metrics
            WHERE category = {cat:String}
              AND metric_date >= {since:Date}
            GROUP BY wb_sku
            ORDER BY total_orders DESC
            LIMIT 200
            """,
            parameters={"cat": category, "since": since},
        ).result_rows

        if not rows:
            return 0.0

        all_orders = [float(r[1]) for r in rows]
        total = sum(all_orders)
        if total == 0:
            return 0.0

        top10 = sum(all_orders[:10])
        return round(top10 / total, 4)


competition_analyzer = CompetitionAnalyzer()
