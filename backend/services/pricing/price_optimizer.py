"""
Оптимизация цен.
Логика: держать цену чуть ниже медианы конкурентов,
но не ниже себестоимости + минимальная маржа.
"""
from core.database import get_clickhouse_client
from schemas.analytics import PricingRecommendation


class PriceOptimizer:
    def __init__(self, min_margin_pct: float = 15.0):
        self._min_margin = min_margin_pct  # минимальная маржа в %

    def recommend(
        self,
        wb_sku: int,
        current_price: float,
        cost_price: float,           # себестоимость
    ) -> PricingRecommendation:
        client = get_clickhouse_client()

        # Медиана и квартили цен конкурентов
        rows = client.query(
            """
            SELECT
                quantile(0.25)(rival_price) AS q25,
                quantile(0.50)(rival_price) AS median,
                quantile(0.75)(rival_price) AS q75
            FROM wb_analytics.competitor_prices
            WHERE our_sku = {sku:UInt64}
              AND snapshot_date >= today() - 3
            """,
            parameters={"sku": wb_sku},
        )

        if not rows.result_rows or rows.result_rows[0][1] is None:
            # Нет данных о конкурентах — держать текущую цену
            return PricingRecommendation(
                wb_sku=wb_sku,
                current_price=current_price,
                recommended_price=current_price,
                min_price=cost_price * (1 + self._min_margin / 100),
                max_price=current_price * 1.2,
                reason="Нет данных о конкурентах",
                expected_revenue_delta=0.0,
            )

        q25, median, q75 = rows.result_rows[0]
        min_price = cost_price * (1 + self._min_margin / 100)

        # Целевая цена: чуть ниже медианы, но не ниже минимума
        target = max(median * 0.97, min_price)

        delta_pct = (target - current_price) / current_price * 100

        if abs(delta_pct) < 2:
            reason = "Цена оптимальна (отклонение < 2%)"
        elif target < current_price:
            reason = f"Снизить цену до уровня рынка (медиана конкурентов: {median:.0f} ₽)"
        else:
            reason = f"Можно повысить цену (вы ниже рынка на {abs(delta_pct):.1f}%)"

        return PricingRecommendation(
            wb_sku=wb_sku,
            current_price=current_price,
            recommended_price=round(target, 0),
            min_price=round(min_price, 0),
            max_price=round(q75, 0),
            reason=reason,
            expected_revenue_delta=round(delta_pct, 1),
        )


price_optimizer = PriceOptimizer()
