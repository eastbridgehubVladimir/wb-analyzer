"""
POST /api/v1/analysis/category

Полный цикл: парсинг WB → метрики → decision engine → результат.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.deps import pg_session
from schemas.analysis import (
    CategoryAnalysisRequest,
    CategoryAnalysisResponse,
    DimensionOut,
    MetricsSummary,
    ScrapedProductOut,
)
from services.analysis.category_analyzer import analyze_category
from services.analysis.inline_metrics import (
    build_competition_report,
    build_price_distribution,
    build_revenue_estimate,
    build_sales_velocity,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.post("/category", response_model=CategoryAnalysisResponse)
async def analyze_category_endpoint(
    payload: CategoryAnalysisRequest,
    db: AsyncSession = Depends(pg_session),
):
    """
    Анализирует товарную нишу Wildberries.

    1. Парсит поисковую выдачу WB по `category`
    2. Парсит каждый найденный товар (цена, рейтинг, отзывы)
    3. Вычисляет 4 метрики ниши
    4. Прогоняет через decision_engine → score 0-100, verdict BUY/TEST/SKIP
    5. Сохраняет данные в PostgreSQL и ClickHouse

    ⚠ Время ответа: 30–120 сек в зависимости от `max_products`.
    Для production вынести в ARQ-воркер и вернуть job_id.
    """
    products, decision = await analyze_category(
        category=payload.category,
        session=db,
        max_products=payload.max_products,
        scrape_pages=payload.scrape_pages,
    )

    # Собираем MetricsSummary из спарсенных данных для ответа
    revenue     = build_revenue_estimate(products, payload.category)
    velocity    = build_sales_velocity(products)
    competition = build_competition_report(products, payload.category)
    prices      = build_price_distribution(products)

    metrics = MetricsSummary(
        monthly_revenue_estimate=revenue.monthly_estimate,
        avg_orders_per_day=velocity.avg_orders_per_day,
        active_sellers=competition.active_sellers,
        competition_level=competition.level.value,
        median_price=prices.median,
        price_iqr=prices.iqr,
        top_20pct_revenue_share=revenue.top_20pct_share,
        top_10_revenue_share=competition.top_10_revenue_share,
    )

    return CategoryAnalysisResponse(
        category=payload.category,
        products_scraped=len(products),
        score=decision.score,
        verdict=decision.verdict.value,
        summary=decision.summary,
        dimensions=[
            DimensionOut(
                name=d.name,
                score=d.score,
                max_score=d.max_score,
                reason=d.reason,
            )
            for d in decision.dimensions
        ],
        metrics=metrics,
        products=[
            ScrapedProductOut(
                wb_sku=p.wb_sku,
                name=p.name,
                brand=p.brand or None,
                price=p.price,
                old_price=p.old_price,
                rating=p.rating,
                reviews_count=p.reviews_count,
                images=p.images,
            )
            for p in products
        ],
    )
