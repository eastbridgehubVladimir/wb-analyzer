from fastapi import APIRouter

from api.v1 import analytics, pricing, products

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(products.router)
api_router.include_router(analytics.router)
api_router.include_router(pricing.router)
