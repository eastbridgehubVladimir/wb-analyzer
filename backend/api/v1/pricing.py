from fastapi import APIRouter, Query

from schemas.analytics import PricingRecommendation
from services.pricing.price_optimizer import price_optimizer

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("/recommend/{wb_sku}", response_model=PricingRecommendation)
async def recommend_price(
    wb_sku: int,
    current_price: float = Query(..., description="Текущая цена в рублях"),
    cost_price: float = Query(..., description="Себестоимость в рублях"),
):
    """Рекомендация оптимальной цены на основе данных конкурентов."""
    return price_optimizer.recommend(wb_sku, current_price, cost_price)
