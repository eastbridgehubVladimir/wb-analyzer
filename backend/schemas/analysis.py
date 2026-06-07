"""
Схемы запроса и ответа для /analysis/category.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class CategoryAnalysisRequest(BaseModel):
    category: str = Field(..., description="Категория или ключевое слово для поиска на WB")
    niche_full: Optional[str] = Field(
        default=None, description="Полное имя ниши из БД (для поиска mpstats_path)"
    )
    max_products: int = Field(
        default=10, ge=1, le=50, description="Максимум товаров для анализа"
    )
    scrape_pages: int = Field(
        default=1, ge=1, le=5, description="Страниц поиска для сбора SKU"
    )
    report_level: str = Field(
        default="standard",
        description="Уровень отчёта: basic | standard | deep",
    )
    pre_computed: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Готовые метрики из браузера — используются напрямую без запросов к MPStats"
    )
    agents: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Результаты агентов из браузера (supplier, ad, docs, content_raw)"
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
    category: str
    products_scraped: int
    score: int
    verdict: str
    summary: str
    dimensions: List[DimensionOut]
    metrics: MetricsSummary
    products: List[ScrapedProductOut]
