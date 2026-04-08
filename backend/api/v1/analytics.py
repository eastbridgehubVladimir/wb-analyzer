from fastapi import APIRouter, Query

from schemas.analytics import DailyMetrics
from services.analytics.demand_analyzer import demand_analyzer

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/demand/{wb_sku}", response_model=list[DailyMetrics])
async def get_demand(
    wb_sku: int,
    days: int = Query(default=30, ge=1, le=365),
):
    """Метрики спроса для товара за последние N дней."""
    return demand_analyzer.get_daily_metrics(wb_sku, days)


@router.get("/trending")
async def get_trending(category: str, limit: int = Query(default=20, le=100)):
    """Топ растущих товаров в категории."""
    skus = demand_analyzer.get_trending_skus(category, limit)
    return {"category": category, "skus": skus}
