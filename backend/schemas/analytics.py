from datetime import date

from pydantic import BaseModel


class DailyMetrics(BaseModel):
    metric_date: date
    wb_sku: int
    views: int
    cart_adds: int
    orders: int
    returns: int
    revenue: float
    avg_price: float
    conversion_rate: float


class PricingRecommendation(BaseModel):
    wb_sku: int
    current_price: float
    recommended_price: float
    min_price: float        # нижняя граница (не уйти в минус)
    max_price: float        # верхняя граница (конкурентная)
    reason: str
    expected_revenue_delta: float  # ожидаемое изменение выручки в %


class CompetitorSnapshot(BaseModel):
    our_sku: int
    rival_sku: int
    rival_price: float
    rival_rating: float
    rival_reviews: int
    rival_position: int
