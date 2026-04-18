"""
Схемы запроса и ответа для /analysis/category.
"""
from pydantic import BaseModel, Field
from typing import Optional, List  # добавляем для поддержки Python 3.9


class CategoryAnalysisRequest(BaseModel):
    category: str = Field(..., description="Категория или ключевое слово для поиска на WB")
    max_products: int = Field(
        default=10, ge=1, le=50, description="Максимум товаров для анализа"
    )
    scrape_pages: int = Field(
        default=1, ge=1, le=5, description="Страниц поиска для сбора SKU"
    )


class ScrapedProductOut(BaseModel):
    wb_sku: int
    name: str
    brand: Optional[str]        # исправлено для Python 3.9
    price: float
    old_price: Optional[float]  # исправлено для Python 3.9
    rating: float
    reviews_count: int
    images: List[str]           # исправлено для Python 3.9


class MetricsSummary(BaseModel):
    monthly_revenue_estimate: float
    avg_orders_per_day: float
    active_sellers: int
    competition_level: str       # LOW / MEDIUM / HIGH / SATURATED
    median_price: float
    price_iqr: float
    top_20pct_revenue_share: float
    top_10_revenue_share: float


class DimensionOut(BaseModel):
    name: str
    score: int
    max_score: int
    reason: str


class CategoryAnalysisResponse(BaseModel):
    pass  # оставляем пустым, пока не нужно
